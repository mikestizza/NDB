#!/usr/bin/env python3
"""
Nutanix Volume Group Cleanup Script

This script identifies and removes Volume Groups (VGs) in a Nutanix environment 
that start with a specified prefix. It performs the following checks and actions:

1. Check if any VMs are attached to the VG (skips the VG unless --force is used)
2. Remove any disks within the VG
3. Delete the VG

Options:
--dry-run: Show what would be deleted without actually making changes
--force: Process VGs even if they have VMs attached (USE WITH CAUTION)
--verbose: Enable more detailed logging
--prefix: Specify the VG name prefix to match (REQUIRED)
--timeout: Set command timeout in seconds (default: 30)
"""

import subprocess
import argparse
import sys
import re
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def run_acli_command(command, dry_run=False, timeout=30, confirm=False):
    """
    Execute an ACLI command via subprocess
    
    Args:
        command (str): The ACLI command to run
        dry_run (bool): If True, only print the command without executing
        timeout (int): Maximum time to wait for command completion in seconds
        confirm (bool): Whether to auto-confirm any prompts with 'yes'
        
    Returns:
        str: Command output if executed, or a dry run message
    """
    full_command = "acli {}".format(command)
    
    # Add echo yes pipe for commands that require confirmation
    if confirm and not dry_run:
        full_command = "echo yes | " + full_command
    
    if dry_run:
        logger.info("[DRY RUN] Would execute: {}".format(full_command))
        return "[DRY RUN] Command not executed"
    
    try:
        logger.info("Executing: {}".format(full_command))
        # Using Popen instead of run for better compatibility with Python 3.6
        process = subprocess.Popen(
            full_command, 
            shell=True, 
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True  # This is equivalent to text=True in Python 3.7+
        )
        
        # Set a timeout for command execution
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            logger.warning("Command timed out after {} seconds, but continuing execution".format(timeout))
            return "TIMEOUT"
        
        if process.returncode != 0:
            logger.error("Command failed with return code: {}".format(process.returncode))
            logger.error("Error output: {}".format(stderr))
            return None
        return stdout
    except Exception as e:
        logger.error("Command failed: {}".format(e))
        return None

def get_volume_groups(dry_run=False):
    """
    List all volume groups using 'acli vg.list'
    
    Returns:
        list: List of volume group names
    """
    output = run_acli_command("vg.list", dry_run)
    if output is None:
        logger.error("Failed to retrieve volume groups")
        return []
    
    if dry_run:
        # For dry run, we might not have actual output, so return empty list
        return []
    
    # Parse the output to extract VG names
    vgs = []
    for line in output.splitlines():
        # Skip header lines or empty lines
        if not line.strip() or line.startswith("-") or "Name" in line:
            continue
        
        # Extract VG name (assuming it's the first column)
        parts = line.split()
        if parts:
            vgs.append(parts[0])
    
    return vgs

def get_vg_vms(vg_name, dry_run=False):
    """
    Get list of VMs attached to a volume group
    
    Args:
        vg_name (str): Name of the volume group
        dry_run (bool): Whether this is a dry run
        
    Returns:
        list: List of VM UUIDs
    """
    output = run_acli_command("vg.get {}".format(vg_name), dry_run)
    if output is None or dry_run:
        return []
    
    # Look for volume_group_attachment_type field to determine if VMs are attached
    attachment_match = re.search(r'volume_group_attachment_type: "([^"]+)"', output)
    if attachment_match:
        attachment_type = attachment_match.group(1)
        if attachment_type == "kNone":
            # No VMs are attached
            return []
        logger.info(f"Volume group {vg_name} has attachment type: {attachment_type}")
    
    # Check for direct attachment VM UUIDs
    vms = []
    attachment_list_entries = re.findall(r'attachment_list\s*{[^}]*vm_uuid:\s*"([^"]+)"[^}]*}', output, re.DOTALL)
    if attachment_list_entries:
        vms.extend(attachment_list_entries)
        logger.info(f"Found directly attached VMs: {attachment_list_entries}")
    
    return vms

def detach_vms(vg_name, vm_uuids, dry_run=False):
    """
    Detach all VMs from a volume group
    
    Args:
        vg_name (str): Name of the volume group
        vm_uuids (list): List of VM UUIDs to detach
        dry_run (bool): Whether this is a dry run
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not vm_uuids:
        logger.info("No VMs to detach from {}".format(vg_name))
        return True
    
    success = True
    for vm_uuid in vm_uuids:
        # Add confirm=True to automatically answer 'yes' to confirmation prompts
        result = run_acli_command("vg.detach_from_vm {} {}".format(vg_name, vm_uuid), 
                                 dry_run, timeout=20, confirm=True)
        
        if result is None and not dry_run:
            logger.error("Failed to detach VM {} from {}".format(vm_uuid, vg_name))
            success = False
        elif result == "TIMEOUT" and not dry_run:
            logger.warning("VM detachment command timed out for VM {} from {}. The operation may still be in progress.".format(
                vm_uuid, vg_name))
            # Consider timeout as success and continue with next operations
        else:
            logger.info("{}Detached VM {} from {}".format(
                "[DRY RUN] Would have " if dry_run else "", vm_uuid, vg_name))
    
    return success

def get_vg_disks(vg_name, dry_run=False):
    """
    Get list of disk indexes attached to a volume group
    
    Args:
        vg_name (str): Name of the volume group
        dry_run (bool): Whether this is a dry run
        
    Returns:
        list: List of disk indexes
    """
    output = run_acli_command("vg.get {}".format(vg_name), dry_run)
    if output is None or dry_run:
        return []
    
    # Parse the output to extract disk indexes
    disks = []
    
    # Using regex to find all disk index entries
    # Look for index: X in disk_list sections
    index_matches = re.findall(r'index: (\d+)', output)
    if index_matches:
        disks.extend(index_matches)
    
    return disks

def detach_disks(vg_name, disk_indexes, dry_run=False):
    """
    Detach all disks from a volume group
    
    Args:
        vg_name (str): Name of the volume group
        disk_indexes (list): List of disk indexes to detach
        dry_run (bool): Whether this is a dry run
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not disk_indexes:
        logger.info("No disks to detach from {}".format(vg_name))
        return True
    
    success = True
    for disk_index in disk_indexes:
        # Use a shorter timeout for disk deletion as it's often an async operation
        # Add confirm=True to automatically answer 'yes' to confirmation prompts
        result = run_acli_command("vg.disk_delete {} {}".format(vg_name, disk_index), 
                                 dry_run, timeout=20, confirm=True)
        
        # Consider TIMEOUT as a success since the operation was likely submitted
        if result is None and not dry_run:
            logger.error("Failed to detach disk index {} from {}".format(disk_index, vg_name))
            success = False
        elif result == "TIMEOUT" and not dry_run:
            logger.warning("Disk deletion command timed out for disk {} in {}. The operation may still be in progress.".format(
                disk_index, vg_name))
            # Consider timeout as success and continue with next operations
    
    return success

def delete_vg(vg_name, dry_run=False):
    """
    Delete a volume group
    
    Args:
        vg_name (str): Name of the volume group to delete
        dry_run (bool): Whether this is a dry run
        
    Returns:
        bool: True if successful, False otherwise
    """
    # Add confirm=True to automatically answer 'yes' to confirmation prompts
    result = run_acli_command("vg.delete {}".format(vg_name), dry_run, timeout=20, confirm=True)
    
    # Consider TIMEOUT as a success as the operation was likely submitted
    success = result is not None or result == "TIMEOUT" or dry_run
    
    if success:
        if dry_run:
            logger.info("[DRY RUN] Would delete volume group: {}".format(vg_name))
        else:
            logger.info("Deleted volume group: {}".format(vg_name))
    else:
        logger.error("Failed to delete volume group: {}".format(vg_name))
    
    return success

def main():
    parser = argparse.ArgumentParser(description="Clean up Nutanix Volume Groups with a specified prefix")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run (no changes made)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force deletion even if VMs are attached (USE WITH CAUTION)")
    parser.add_argument("--prefix", required=True, help="VG name prefix to match")
    parser.add_argument("--timeout", type=int, default=30, help="Command timeout in seconds (default: 30)")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Print run mode
    if args.dry_run:
        logger.info("Running in DRY RUN mode - no changes will be made")
    else:
        logger.info("Running in LIVE mode - changes will be applied")
        
    if args.force:
        logger.warning("FORCE mode enabled - will attempt to delete VGs even if VMs are attached")
        
    logger.info("Targeting VGs with prefix: {}".format(args.prefix))
    logger.info("Command timeout set to {} seconds".format(args.timeout))
    
    # Get list of all volume groups
    logger.info("Retrieving list of volume groups...")
    vgs = get_volume_groups(args.dry_run)
    
    if args.dry_run:
        # For dry run, parse output from acli vg.list command if possible
        # Try to execute the command just to see the output, but don't rely on the result
        try:
            output = run_acli_command("vg.list", dry_run=False)
            if output:
                # Parse the real VGs from the output even in dry run mode
                vgs = []
                for line in output.splitlines():
                    parts = line.split()
                    if parts and not line.startswith("Volume Group") and not line.startswith("-"):
                        vgs.append(parts[0])
            else:
                # Fallback to simulated data if we can't get real data
                logger.info("[DRY RUN] Using simulated VG list for demonstration")
                vgs = ["EXAMPLE_VG1", "EXAMPLE_VG2", "OTHER_VG", "ANOTHER_VG"]
        except Exception as e:
            # Fallback to simulated data if command fails
            logger.info("[DRY RUN] Using simulated VG list for demonstration")
            vgs = ["EXAMPLE_VG1", "EXAMPLE_VG2", "OTHER_VG", "ANOTHER_VG"]
    
    if not vgs:
        if not args.dry_run:
            logger.info("No volume groups found or unable to retrieve the list")
            return
    
    logger.info("Found {} volume groups".format(len(vgs)))
    
    # Filter for volume groups that start with the specified prefix
    target_vgs = [vg for vg in vgs if vg.startswith(args.prefix)]
    
    if not target_vgs:
        logger.info("No volume groups found matching the pattern '{}'".format(args.prefix))
        return
    
    logger.info("Found {} volume groups matching the pattern '{}'".format(len(target_vgs), args.prefix))
    
    # Process each target volume group
    success_count = 0
    failure_count = 0
    
    for vg_name in target_vgs:
        logger.info("Processing volume group: {}".format(vg_name))
        
        # Check for attached VMs
        if args.dry_run:
            logger.info("[DRY RUN] Would check for VMs attached to {}".format(vg_name))
            # Try to get actual VM attachment information even in dry run mode
            try:
                output = run_acli_command("vg.get {}".format(vg_name), dry_run=False)
                if output:
                    # Check attachment type
                    attachment_match = re.search(r'volume_group_attachment_type: "([^"]+)"', output)
                    if attachment_match and attachment_match.group(1) != "kNone":
                        # Look for attached VMs
                        vm_uuids = re.findall(r'attachment_list\s*{[^}]*vm_uuid:\s*"([^"]+)"[^}]*}', output, re.DOTALL)
                        if vm_uuids:
                            logger.info("[DRY RUN] Found VMs attached to {}: {}".format(vg_name, ", ".join(vm_uuids)))
                            attached_vms = vm_uuids
                        else:
                            attached_vms = []
                    else:
                        attached_vms = []
                else:
                    attached_vms = []
            except Exception:
                attached_vms = []
        else:
            attached_vms = get_vg_vms(vg_name, args.dry_run)
        
        # If VMs are attached
        if attached_vms:
            if args.force:
                logger.warning("VMs are attached to {}: {}. Continuing with detachment due to --force flag".format(
                    vg_name, ", ".join(attached_vms)))
                # Detach VMs first
                if not detach_vms(vg_name, attached_vms, args.dry_run):
                    logger.error("Failed to detach VMs from {}. Skipping VG deletion.".format(vg_name))
                    failure_count += 1
                    continue
            else:
                logger.warning("VMs are attached to {}: {}. Skipping (use --force to override)".format(
                    vg_name, ", ".join(attached_vms)))
                failure_count += 1
                continue
        
        # Get disks attached to the VG
        if args.dry_run:
            logger.info("[DRY RUN] Would check for disks in {}".format(vg_name))
            # Try to get actual disk information even in dry run mode for better simulation
            try:
                output = run_acli_command("vg.get {}".format(vg_name), dry_run=False)
                if output:
                    disk_indexes = []
                    # Using regex to find all disk index entries
                    index_matches = re.findall(r'index: (\d+)', output)
                    if index_matches:
                        disk_indexes = index_matches
                    logger.info("[DRY RUN] Found {} disks in {}".format(len(disk_indexes), vg_name))
                else:
                    disk_indexes = ["0"]  # Default to one disk with index 0
            except Exception:
                disk_indexes = ["0"]  # Default to one disk with index 0
        else:
            disk_indexes = get_vg_disks(vg_name, args.dry_run)
            logger.info("Found {} disks attached to {}".format(len(disk_indexes), vg_name))
        
        # Detach disks
        if detach_disks(vg_name, disk_indexes, args.dry_run):
            # Delete the VG
            if delete_vg(vg_name, args.dry_run):
                success_count += 1
            else:
                failure_count += 1
        else:
            logger.error("Skipping deletion of {} due to disk detachment failure".format(vg_name))
            failure_count += 1
    
    # Summary
    logger.info("=" * 50)
    logger.info("Operation Summary:")
    logger.info("Total VGs matching pattern: {}".format(len(target_vgs)))
    logger.info("Successfully processed: {}".format(success_count))
    logger.info("Failed: {}".format(failure_count))
    logger.info("=" * 50)
    
    if args.dry_run:
        logger.info("This was a dry run. No actual changes were made.")

if __name__ == "__main__":
    main()
