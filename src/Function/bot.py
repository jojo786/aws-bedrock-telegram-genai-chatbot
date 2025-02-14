import boto3
import json
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from datetime import datetime, timedelta
import os
from aws_lambda_powertools.utilities import parameters
import time

# Initialize SSM client
ssm = boto3.client('ssm')
# Initialize Bedrock client
bedrock = boto3.client('bedrock-runtime')
# Get table name from environment variable
CHAT_HISTORY_TABLE = os.environ['CHATHISTORY_TABLE_NAME']
# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(CHAT_HISTORY_TABLE)

# Get the Telegram bot token from Parameter Store
ssm_provider = parameters.SSMProvider()
TelegramBotToken = ssm_provider.get('/bedrock-telegram-genai-chatbot/telegram/prod/bot_token', decrypt=True)

# Initialize PTB
application = ApplicationBuilder().token(TelegramBotToken).build()

model_id = "anthropic.claude-3-sonnet-20240229-v1:0"
# Inference parameters to use.
temperature = 0.5
top_k = 200

# Setup the system prompts and messages to send to the model.
system_prompts = [{"text": "You are an conversational chat bot, that users are using from the Telegram application."}]

# Base inference parameters to use.
inference_config = {"temperature": temperature}
# Additional inference parameters to use.
additional_model_fields = {"top_k": top_k}

class BotHandler:
    def __init__(self, lambda_context):
        self.lambda_context = lambda_context
        self.start_time = time.perf_counter()  # More precise timing

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current_time = time.perf_counter()
        execution_duration = current_time - self.start_time
        remaining_time = self.lambda_context.get_remaining_time_in_millis() / 1000  # Convert ms to seconds
        function_version = self.lambda_context.function_version
        
        await update.message.reply_text(
            f"Bot is running!\n"
            f"Function version: {function_version}\n"
            f"Execution duration: {execution_duration} seconds\n"
            f"Remaining time until timeout: {remaining_time:.3f} seconds"
        )


async def get_chat_history(chat_id):
    response = table.query(
        KeyConditionExpression='chat_id = :chat_id',
        ExpressionAttributeValues={':chat_id': str(chat_id)},
        ScanIndexForward=False,  # Get most recent messages first
    )
    return response.get('Items', [])

async def save_message(chat_id, role, content):
    current_time = datetime.utcnow()
    timestamp = current_time.isoformat()
    
    # Calculate TTL (current time + 1 hour) in epoch seconds
    ttl = int((current_time + timedelta(hours=1)).timestamp())
    
    table.put_item(Item={
        'chat_id': str(chat_id),
        'timestamp': timestamp,
        'role': role,
        'content': content,
        'expireat': ttl  # TTL attribute
    })



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a GenAI chatbot running on AWS Serverless, please talk to me!")

async def bedrock_converse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    messages = []
    
    chat_id = update.effective_chat.id
    user_message = update.message.text
    #print(user_message)

    # Get recent chat history
    chat_history = await get_chat_history(chat_id)
   
   # Save user message
    await save_message(chat_id, 'user', user_message)
    
    # Build conversation context
    conversation = ""
    for msg in reversed(chat_history):  # Oldest to newest
        conversation = {
            "role": msg['role'],
            "content": [{"text": msg['content']}]
            }
        #print(conversation)
        messages.append(conversation)

    message =  {
                "role": "user", 
                "content": [{f"text": user_message}]
        }
    
    messages.append(message)
    #print(messages)

    # Call Bedrock Converse API
    response = bedrock.converse(
        modelId=model_id,
        messages=messages,
        system=system_prompts,
        inferenceConfig=inference_config,
        additionalModelRequestFields=additional_model_fields
    )
    print(response)
    # Parse response
    #response_body = json.loads(response['output'].read())
    
    # Parse response - response is already a dictionary
    bedrock_response = response['output']['message']['content'][0]['text']
    bedrock_response_metrics = response['metrics']['latencyMs']
    bedrock_response_usage = response['usage']

    # Save bedrock response
    await save_message(chat_id, 'assistant', bedrock_response)

    # Send response to telegram
    ptb_response_message = await context.bot.send_message(chat_id=update.effective_chat.id, text=bedrock_response)

    await context.bot.send_message(chat_id=update.effective_chat.id, reply_to_message_id=ptb_response_message.message_id, text=f"Debug: \n Bedrock Response time: {bedrock_response_metrics / 1000} sec \n Bedrock Usage: {bedrock_response_usage}") 
    #await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Bedrock Usage: {bedrock_response_usage}")

def lambda_handler(event, context):
    return asyncio.get_event_loop().run_until_complete(main(event, context))

async def main(event, context):
    # Create bot handler with Lambda context
    bot_handler = BotHandler(context)

    # Register command handler with the instance method that has access to lambda_context
    application.add_handler(CommandHandler('status', bot_handler.status_command))

    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)

    bedrock_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), bedrock_converse)
    application.add_handler(bedrock_handler)
    
    try:    
        await application.initialize()
        await application.process_update(
            Update.de_json(json.loads(event["body"]), application.bot)
        )
    
        return {
            'statusCode': 200,
            'body': 'Success'
        }

    except Exception as exc:
        return {
            'statusCode': 500,
            'body': 'Failure'
        }