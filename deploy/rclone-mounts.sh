#!/bin/bash
# Ensure the rclone mounts are up; safe to run often (cron: @reboot and */5).
# Healthy mounts are left alone; dead/missing ones are (re)mounted.
exec 9>/tmp/rclone-mounts.lock
flock -n 9 || exit 0

RCLONE=/usr/bin/rclone
LOG=/home/dcr/rclone-mounts.log

ensure() {
    local remote="$1" mnt="$2"; shift 2
    # a stale FUSE mount makes mountpoint/ls fail; detach it first
    if ! mountpoint -q "$mnt"; then
        fusermount -uz "$mnt" 2>/dev/null
        echo "$(date '+%F %T') mounting $remote on $mnt" >> "$LOG"
        "$RCLONE" mount "$remote" "$mnt" --daemon --allow-non-empty \
            --log-file "$LOG" -v "$@" 9>&-
    elif ! timeout 20 ls "$mnt" >/dev/null 2>&1; then
        echo "$(date '+%F %T') $mnt unresponsive, remounting" >> "$LOG"
        fusermount -uz "$mnt" 2>/dev/null
        "$RCLONE" mount "$remote" "$mnt" --daemon --allow-non-empty \
            --log-file "$LOG" -v "$@" 9>&-
    fi
}

ensure backblaze:cowardhourstorage /mnt/bb
ensure backblaze:podcasts-storage  /mnt/podcasts --vfs-cache-mode writes
ensure backblaze:doomscrollstorage /mnt/doomscroll
