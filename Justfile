image := "ghcr.io/tritacle/ntnu-room-booking:latest"
service := "koker-book"

# Symlink systemd units from project into user scope
link:
    ln -sf "$(pwd)/systemd/koker-book-docker.service" ~/.config/systemd/user/koker-book.service
    ln -sf "$(pwd)/systemd/koker-book.timer" ~/.config/systemd/user/koker-book.timer
    ln -sf "$(pwd)/systemd/koker-warm.service" ~/.config/systemd/user/koker-warm.service
    ln -sf "$(pwd)/systemd/koker-warm.timer" ~/.config/systemd/user/koker-warm.timer
    ln -sf "$(pwd)/systemd/koker-notify-failure.service" ~/.config/systemd/user/koker-notify-failure.service
    systemctl --user daemon-reload

# Enable and start the daily timer
enable:
    systemctl --user enable --now koker-book.timer

# Disable the timer
disable:
    systemctl --user disable --now koker-book.timer

# Stream live log
logs:
    journalctl --user -u koker-book.service -f

# Systemd service/timer status
status:
    systemctl --user status koker-book.timer koker-book.service

# Next scheduled fire time
next:
    systemctl --user list-timers koker-book.timer

# Trigger one booking run now (blocks until done)
run:
    systemctl --user start koker-book.service

# Pull the latest image manually
pull:
    docker pull {{image}}

# Copy fresh session from laptop to server (run after `koker login` on laptop)
sync:
    scp data/storage_state.json server:srv/ntnu-room-booking/data/storage_state.json

# Trigger session warm run now
warm:
    systemctl --user start koker-warm.service
