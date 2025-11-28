#!/bin/bash

# Build the Docker image
echo "Building Docker image..."
docker build -t recruitment-bot .

# Check if build was successful
if [ $? -ne 0 ]; then
    echo "Docker build failed!"
    exit 1
fi

# Prompt for Token
echo ""
echo "Please enter your Telegram Bot Token:"
read -s BOT_TOKEN

if [ -z "$BOT_TOKEN" ]; then
    echo "Token cannot be empty."
    exit 1
fi

# Run the container
echo ""
echo "Starting Bot Container..."
docker run -d \
    --name recruitment-bot-instance \
    --restart unless-stopped \
    -e TELEGRAM_BOT_TOKEN="$BOT_TOKEN" \
    -v $(pwd)/bot_database.db:/app/bot_database.db \
    recruitment-bot

echo ""
echo "Bot is running in the background!"
echo "Check status with: docker ps"
echo "View logs with: docker logs -f recruitment-bot-instance"
