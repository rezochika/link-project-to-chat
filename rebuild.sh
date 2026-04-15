#!/bin/bash
set -e
sudo -u botuser pip install -e "/home/botuser/.link-project-to-chat/repos/link-project-to-chat[all]" --break-system-packages -q --no-warn-script-location
sudo systemctl restart link-project-to-chat
echo "Rebuilt and restarted."
