#!/bin/bash

# Get the list of all screen sessions
screen_sessions=$(screen -ls | grep 'Detached' | awk '{print $1}')

# Loop through each session and kill it
for session in $screen_sessions; do
    screen -S "$session" -X quit
done

echo "All screen sessions killed."
