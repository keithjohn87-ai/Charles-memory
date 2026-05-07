#!/bin/bash
# Send an iMessage to John. Usage: send_imessage.sh "<message text>"
set -e
MSG="$1"
TARGET="+16156637932"

if [ -z "$MSG" ]; then
    echo "usage: $0 <message>" >&2
    exit 1
fi

osascript <<APPLESCRIPT
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "$TARGET" of targetService
    send "$MSG" to targetBuddy
end tell
APPLESCRIPT
