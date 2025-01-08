import boto3
import json
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from datetime import datetime, timedelta
import os
from aws_lambda_powertools.utilities import parameters

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
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot running on AWS Serverless, please talk to me!")

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
    print(messages)

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
    #bedrock_response = response_body['content'][0]['text']
    
    # Parse response - response is already a dictionary
    bedrock_response = response['output']['message']['content'][0]['text']

    # Save bedrock response
    await save_message(chat_id, 'assistant', bedrock_response)

    # Send response to telegram
    await context.bot.send_message(chat_id=update.effective_chat.id, text=bedrock_response)

def lambda_handler(event, context):
    return asyncio.get_event_loop().run_until_complete(main(event, context))




async def main(event, context):
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
    
   

