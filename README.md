# AWS Bedrock Telegram GenAI Chatbot
Telegram conversational GenAI bot, powered by Amazon Bedrock, using PTB, hosted on AWS Serverless

- Uses the [python-telegram-bot](https://pypi.org/project/python-telegram-bot/) telegram bot framework, using [this project](https://github.com/jojo786/Sample-Python-Telegram-Bot-AWS-Serverless-PTBv20) to run PTB on AWS Serverless
- GenAI conversational capabilities powered by Amazon Bedrock, using the Claude Sonnet 4.5 LLM
- Supports response streaming for real-time chat experience, using APIGateway Response Streaming and Lambda response streaming with FastAPI and Lambda Web Adapter 

# Architecture
Requests from Telegram come in via an Amazon API Gateway endpoint with response streaming enabled, which get routed to a Lambda function running FastAPI with Lambda Web Adapter. The Lambda function gets the Telegram Token and API Secret Token from SSM Parameter Store for secure authentication. Requests are sent to Amazon Bedrock with support for response streaming and chain of thought reasoning. Chat history and user settings are maintained in DynamoDB with TTL and Global Secondary Index for efficient querying. Logs are stored on CloudWatch. All deployed using AWS SAM IaC. Monitoring services like X-Ray, Lambda Insights and Application Signal are all enabled for comprehensive monitoring.

![architecture](docs/telegram-bedrock-architecture.png)

# Features

## Core Capabilities
- **Conversational AI**: Natural language conversations powered by Claude Sonnet 4.5
- **Document Analysis**: Upload and analyze PDF, DOC, DOCX, and TXT files (up to 4.5MB)
- **Chat History**: Persistent conversation history with automatic 1-hour TTL
- **Response Streaming**: Real-time streaming responses for better user experience

## Bot Commands
- `/start` - Initialize the bot and get welcome message
- `/status` - Show bot health, Lambda function version, execution time, and remaining timeout
- `/debug` - Toggle debug mode to show Bedrock response times and usage metrics
- `/thinking` - Enable/disable Claude's chain of thought reasoning display
- `/clear` - Delete all chat history for the current user

## Advanced Features
- **Chain of Thought**: See Claude's reasoning process when enabled
- **Security**: Telegram API Secret Token validation for webhook security
- **Multi-region Support**: Supports different Claude model regions (US, AF, Global)
- **Auto-cleanup**: Chat history and settings automatically expire after 1 hour
- **Health Monitoring**: Built-in health check endpoint for monitoring
- **Comprehensive Logging**: Detailed logging with CloudWatch integration
# How it works
- A Telegram bot has been created with the webhook URL set to point to the Amazon API Gateway endpoint. Now when ever a user interacts with the bot, the requests are send to API GW
- API Gateway receives the request and forwards to a Lambda function
- The Lambda function gets invoked and does a few things:
- - retrieves the Telegram token from SSM
- - manages the chat history, by storing the new chat request in DynamoDB, then retrieving the previous requests to build up the whole chat histor
- - sends the request to Bedrock, and parses the response
- - Saves the response in DynamoDB to main chat history
- - Sends the response back to the Telegram user
![service map](docs/telegram-bedrock-service-map.png)

# How to run it
- Create your bot using [BotFather](https://core.telegram.org/bots#3-how-do-i-create-a-bot), and note the token, e.g. `12334342:ABCD124324234`
- Install [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html), and  [configure it](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-quickstart.html#cli-configure-quickstart-config)
- Install [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html)
- Create SSM Parameters to store the Telegram credentials:
  - Bot token: `aws ssm put-parameter --region us-east-1 --name "/bedrock-telegram-genai-chatbot/telegram/prod/bot_token" --type "SecureString" --value "12334342:ABCD12432423" --overwrite`
  - API Secret token: `aws ssm put-parameter --region us-east-1 --name "/bedrock-telegram-genai-chatbot/telegram/prod/api_secret_token" --type "SecureString" --value "your-secret-token" --overwrite`
- Run `sam build && sam deploy --guided`. Provide a stack-name, and use `us-east-1` as the region (mostly due to Bedrock). 
- Note the Outputs from the above `sam deploy` command, which will include the Value of the TelegramApi, which is the API GW / Lambda URL endpoint, e.g. `https://1fgfgfd56.lambda-url.eu-west-1.on.aws/` 
- Update your Telegram bot to change from polling to [Webhook](https://core.telegram.org/bots/api#setwebhook), by pasting this URL in your browser, or curl'ing it: `https://api.telegram.org/bot12334342:ABCD124324234/setWebHook?url=https://1fgfgfd56.lambda-url.eu-west-1.on.aws/bot&secret_token=your-secret-token`. Use your bot token, API GW endpoint, and secret token. You can check that it was set correctly by going to `https://api.telegram.org/bot12334342:ABCD124324234/getWebhookInfo`, which should include the `url` of your API GW endpoint, as well as any errors Telegram is encountering calling your bot on that webhook.

# Usage Examples

## Basic Chat
Just send any message to start a conversation with Claude.

## Document Analysis
1. Upload a PDF, DOC, DOCX, or TXT file (max 4.5MB)
2. The bot will automatically analyze the document and provide insights
3. Citations will be included when available

## Debug Mode
1. Send `/debug` to toggle debug mode
2. When enabled, you'll see Bedrock response times and token usage
3. Send `/debug` again to disable

## Chain of Thought
1. Send `/thinking` to enable Claude's reasoning display
2. You'll see Claude's thought process before the final response
3. Send `/thinking` again to disable

## Managing Chat History
- Send `/clear` to delete all your chat history
- History automatically expires after 1 hour for privacy