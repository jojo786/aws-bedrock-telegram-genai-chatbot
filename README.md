# AWS Bedrock Telegram GenAI Chatbot
Telegram conversational GenAI bot, powered by Amazon Bedrock, using PTB, hosted on AWS Serverless

- Uses the [python-telegram-bot](https://pypi.org/project/python-telegram-bot/) telegram bot framework, using [this project](https://github.com/jojo786/Sample-Python-Telegram-Bot-AWS-Serverless-PTBv20) to run PTB on AWS Serverless
- GenAI conversational capabilities powered by Amazon Bedrock, using the Claude 3.5 Sonnet LLM

# Architecture
Requests from Telegram come in via an Amazon API Gateway endpoint, which get routed to a Lambda function. The Lambda function gets the Telegram Token from SSM Parameter Store. Requests are sent to Amazon Bedrock. Chat history is maintained in DynamoDB. Logs are stored on CloudWatch. Telegram token stored securely in SSM. All deployed using AWS SAM IaC. [Lambda SnapStart](https://hacksaw.co.za/blog/aws-lambda-snapstart-for-python/) is enabled to reduce cold starts and improve performance. Monitoring services like X-Ray, Lambda Insights and Application Signal are all enabled for comprehensive monitoring.

![architecture](docs/telegram-bedrock-architecture.png)

![service map](docs/telegram-bedrock-service-map.png)