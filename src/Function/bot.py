import boto3
import json
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from datetime import datetime, timedelta
import os
from aws_lambda_powertools.utilities import parameters
import time
import re


# Initialize SSM client
ssm = boto3.client('ssm')
# Initialize Bedrock client for synchronous operations
bedrock = boto3.client('bedrock-runtime')
# Get table name from environment variable
CHAT_HISTORY_TABLE = os.environ['CHATHISTORY_TABLE_NAME']
# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(CHAT_HISTORY_TABLE)

# Get the Telegram bot token from Parameter Store
ssm_provider = parameters.SSMProvider()
TelegramBotToken = ssm_provider.get('/bedrock-telegram-genai-chatbot/telegram/prod/bot_token', decrypt=True)
TelegramBotAPISecretToken = ssm_provider.get('/bedrock-telegram-genai-chatbot/telegram/prod/api_secret_token', decrypt=True)

# Initialize PTB
application = ApplicationBuilder().token(TelegramBotToken).build()

# Claude Sonnet 4.5 model configuration for AWS Bedrock
model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
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
            FilterExpression='record_type = :type',
            ExpressionAttributeValues={
                ':chat_id': str(chat_id),
                ':type': 'CHAT_MESSAGE'
            },
            ScanIndexForward=False  # This will get the most recent messages first
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
        'record_type': 'CHAT_MESSAGE',  # Add record_type
        'role': role,
        'content': content,
        'expireat': ttl  # TTL attribute
    })



async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a GenAI chatbot, powered by Amazon Bedrock, running on AWS Serverless, please talk to me!")

async def bedrock_converse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    messages = []
    
    chat_id = update.effective_chat.id
    user_message = update.message.text
    current_time = await get_current_datetime()
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

    # Add current time context to the user message
    message = {
        "role": "user", 
        "content": [
            {"text": f"This is the current time, but you dont need to mention it in your response unless required: {current_time}\nUser message: {user_message}"}
        ]
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

    # Check debug status before sending debug message
    debug_enabled = await get_debug_status(update.effective_chat.id)
    print(f"Checking the debug status before sending debug messages: {debug_enabled}")
    if debug_enabled:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            reply_to_message_id=ptb_response_message.message_id, 
            text=f"Debug: \n Bedrock Response time: {bedrock_response_metrics / 1000} sec \n Bedrock Usage: {bedrock_response_usage}"
        )
    

def sanitize_filename(filename):
    # Remove file extension first
    base_name = os.path.splitext(filename)[0]
    
    # Replace invalid characters with spaces
    # Keep only alphanumeric, spaces, hyphens, parentheses, and square brackets
    sanitized = re.sub(r'[^a-zA-Z0-9\s\-\(\)\[\]]', ' ', base_name)
    
    # Replace multiple spaces with single space
    sanitized = re.sub(r'\s+', ' ', sanitized)
    
    # Trim spaces from start and end
    sanitized = sanitized.strip()
    
    return sanitized


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Start processing document: ")
    
    chat_id = update.effective_chat.id
    document = update.effective_message.document
    mime_type = document.mime_type
    file_name = document.file_name
    file_size = document.file_size  # Size in bytes
    
    # Define size limit (4.5MB in bytes)
    SIZE_LIMIT = 4.5 * 1024 * 1024  # 4.5MB in bytes
    
    # Check file size
    if file_size > SIZE_LIMIT:
        size_mb = file_size / (1024 * 1024)  # Convert to MB for user-friendly message
        print("file too large")
        # Send response to telegram
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"File is too large ({size_mb:.2f}MB). Maximum allowed size is 4.5MB. Please upload a smaller file."
        )
        return

    
    # Log document details
    print(f"File name: {file_name}")
    print(f"MIME type: {mime_type}")
    print(f"File size: {file_size / (1024 * 1024):.2f}MB")
    

    
    # Define supported document types
    supported_types = {
        'application/pdf': {
            'extension': '.pdf',
            'type': 'PDF'
        },
        'application/msword': {
            'extension': '.doc',
            'type': 'DOC'
        },
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': {
            'extension': '.docx',
            'type': 'DOCX'
        },
        'text/plain': {
            'extension': '.txt',
            'type': 'TXT'
        }
    }
    
    # Check if document type is supported
    if mime_type not in supported_types:
        print("Unsupported document type")
        # Send response to telegram
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this file type is not supported. Please upload a PDF, DOC, DOCX, or TXT file."
        )
        return
    
    try:
        # Get the file
        file = await context.bot.getFile(document.file_id)
        file_type = supported_types[mime_type]
        temp_file_path = f'/tmp/doc{file_type["extension"]}'
        
        # Download the file
        await file.download_to_drive(temp_file_path)
        
        # Read the file contents
        with open(temp_file_path, 'rb') as doc:
            doc_contents = doc.read()
        
        print(f"Successfully saved document as {file_type['type']}")

                # Sanitize the filename for Bedrock
        sanitized_name = sanitize_filename(file_name)
        print(f"Sanitized filename: {sanitized_name}")
        
        # Ensure we have a valid filename after sanitization
        if not sanitized_name:
            sanitized_name = "document"

        
        # Prepare messages for Bedrock Converse API
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": f"Please analyze this document"
                    },
                    {
                        "document": {
                            "format": file_type['extension'].lstrip('.'),
                            "name": sanitized_name,
                            "source": {
                                "bytes": doc_contents
                            }
                        }
                    }
                ]
            }
        ]

        # Use the converse API with consistent model configuration
        response = bedrock.converse(
            modelId=model_id,
            messages=messages,
            inferenceConfig=inference_config,
            additionalModelRequestFields=additional_model_fields
        )
        
        # Extract the text from the response
        bedrock_response = response['output']['message']['content'][0]['text']
        bedrock_response_metrics = response['metrics']['latencyMs']
        bedrock_response_usage = response['usage']

        # Send response to telegram
        ptb_response_message = await context.bot.send_message(
            chat_id=chat_id, 
            text=bedrock_response
        )

        # Check debug status before sending debug message
        debug_enabled = await get_debug_status(update.effective_chat.id)
        print(f"Checking the debug status before sending debug messages: {debug_enabled}")
        if debug_enabled:
            await context.bot.send_message(
                chat_id=chat_id, 
                reply_to_message_id=ptb_response_message.message_id, 
                text=f"Debug: \n Bedrock Response time: {bedrock_response_metrics / 1000} sec \n Bedrock Usage: {bedrock_response_usage}"
            )
        
    except Exception as e:
        error_message = f"Error processing document: {str(e)}"
        print(error_message)
        await context.bot.send_message(chat_id=chat_id, text=error_message)
    
    finally:
        # Cleanup: Remove temporary file if it exists
        if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

async def debug_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Get current debug status
    current_status = await get_debug_status(chat_id)
    
    # Toggle the status
    new_status = not current_status
    
    # Save the new status
    if await save_debug_status(chat_id, new_status):
        status_text = "enabled" if new_status else "disabled"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Debug messages have been {status_text}."
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Failed to update debug settings."
        )

async def get_debug_status(chat_id):
    try:
        response = table.query(
            IndexName='DebugSettingsIndex',
            KeyConditionExpression='chat_id = :chat_id AND record_type = :type',
            ExpressionAttributeValues={
                ':chat_id': str(chat_id),
                ':type': 'DEBUG_SETTINGS'
            },
            ScanIndexForward=False,  # Get most recent first
            Limit=1  # We only need the most recent setting
        )
        items = response.get('Items', [])
        print(f"Debug items: {items}")  # Add logging for debugging
        return items[0].get('debug_enabled', False) if items else False
    except Exception as e:
        print(f"Error getting debug status: {e}")
        return False

async def save_debug_status(chat_id, status):
    try:
        current_time = datetime.utcnow()
        timestamp = current_time.isoformat()
        # Calculate TTL (current time + 1 year) in epoch seconds
        ttl = int((current_time + timedelta(days=365)).timestamp())
        
        table.put_item(Item={
            'chat_id': str(chat_id),
            'timestamp': timestamp,
            'record_type': 'DEBUG_SETTINGS',
            'debug_enabled': status,
            'expireat': ttl
        })
        return True
    except Exception as e:
        print(f"Error saving debug status: {e}")
        return False

def lambda_handler(event, context):
    # Check if secret token header exists and matches expected value
    if 'headers' not in event or \
       'X-Telegram-Bot-Api-Secret-Token' not in event['headers'] or \
       event['headers']['X-Telegram-Bot-Api-Secret-Token'] != TelegramBotAPISecretToken:
        print("Unauthorized - Telegram API Secret Token Header not found")
        return {
            'statusCode': 401,
            'body': 'Unauthorized'
        }
    
    return asyncio.get_event_loop().run_until_complete(main(event, context))

#clear chat history
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    try:
        # First, get all items for this chat_id where record_type is CHAT_MESSAGE
        response = table.query(
            KeyConditionExpression='chat_id = :chat_id',
            FilterExpression='record_type = :type',
            ExpressionAttributeValues={
                ':chat_id': str(chat_id),
                ':type': 'CHAT_MESSAGE'
            }
        )
        
        items = response.get('Items', [])
        
        if not items:
            await context.bot.send_message(
                chat_id=chat_id,
                text="No chat history found to clear."
            )
            return
        
        # DynamoDB batch_write_item can only handle 25 items at a time
        batch_size = 25
        deleted_count = 0
        
        for i in range(0, len(items), batch_size):
            batch_items = items[i:i + batch_size]
            
            # Prepare batch delete request
            delete_requests = [
                {
                    'DeleteRequest': {
                        'Key': {
                            'chat_id': item['chat_id'],
                            'timestamp': item['timestamp']
                        }
                    }
                }
                for item in batch_items
            ]
            
            # Execute batch delete
            dynamodb.batch_write_item(
                RequestItems={
                    CHAT_HISTORY_TABLE: delete_requests
                }
            )
            
            deleted_count += len(batch_items)
        
        # Send confirmation message
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Successfully cleared {deleted_count} messages from your chat history."
        )
        
        # Send a follow-up message to start fresh
        await context.bot.send_message(
            chat_id=chat_id,
            text="You can start a new conversation now."
        )
        
    except Exception as e:
        print(f"Error clearing chat history: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, I encountered an error while trying to clear your chat history."
        )

async def get_current_datetime():
    """Get current date and time in a formatted string"""
    current = datetime.utcnow()
    return current.strftime("%Y-%m-%d %H:%M:%S UTC")

async def main(event, context):
    # Create bot handler with Lambda context
    bot_handler = BotHandler(context)

    # Register command handler with the instance method that has access to lambda_context
    application.add_handler(CommandHandler('status', bot_handler.status_command))

    application.add_handler(CommandHandler('start', start_command))

    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), bedrock_converse))

    #Add the debug toggle command handler
    application.add_handler(CommandHandler('debug', debug_handler))

    # Add the clear chat history command handler
    application.add_handler(CommandHandler('clear', clear_command))
    
    # Add these handlers to catch different document types
    application.add_handler(MessageHandler(
        filters.Document.PDF |
        filters.Document.DOC |
        filters.Document.DOCX |
        filters.Document.TXT,
        document_handler
    ))
    application.add_handler(MessageHandler(
        filters.Document.MimeType("application/pdf") |
        filters.Document.MimeType("application/msword") |
        filters.Document.MimeType("application/vnd.openxmlformats-officedocument.wordprocessingml.document") |
        filters.Document.MimeType("text/plain"),
        document_handler
    ))
    

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