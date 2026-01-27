import boto3
import json
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from datetime import datetime, timedelta
import os
from aws_lambda_powertools.utilities import parameters
from aws_lambda_powertools import Logger
import time
import uuid
from fastapi import FastAPI, Request
import re
from fastapi.responses import JSONResponse

bot = FastAPI()

@bot.get("/")
def health_check():
    return {"status": "healthy", "message": "Telegram bot is running"}


# Initialize Logger
logger = Logger()

# Initialize SSM client
ssm = boto3.client('ssm')
# Initialize Bedrock client for synchronous operations
bedrock = boto3.client('bedrock-runtime', region_name=os.environ.get('REGION', 'af-south-1'))
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

#model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
#model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
#model_id = "af.anthropic.claude-sonnet-4-5-20250929-v1:0"
model_id = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Inference parameters to use.
temperature = 1 #0.5
top_k = 200

# Setup the system prompts and messages to send to the model.
system_prompts = [{"text": "You are an conversational chat bot, that users are using from the Telegram application."}]

# Base inference parameters to use.
inference_config = {"temperature": temperature}
# Additional inference parameters to use.
additional_model_fields = {
    #"top_k": top_k
}

# Global variables for timing
start_time = None
lambda_context = None

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_time = time.perf_counter()
    execution_duration = current_time - start_time if start_time else 0
    remaining_time = lambda_context.get_remaining_time_in_millis() / 1000 if lambda_context else 0
    function_version = lambda_context.function_version if lambda_context else "unknown"
    
    await update.message.reply_text(
        f"Bot is running!\n"
        f"Function version: {function_version}\n"
        f"Execution duration: {execution_duration:.3f} seconds\n"
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

async def bedrock_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Processing message from chat_id: {update.effective_chat.id}, user: {update.effective_user.id}")
    logger.debug(f"Full Telegram Update object: {update}")
    
    chat_id = update.effective_chat.id
    user_message = update.message.text
    message_thread_id = update.message.message_thread_id

    current_time = await get_current_datetime()

    # Get recent chat history
    chat_history = await get_chat_history(chat_id)
   
    # Save user message
    await save_message(chat_id, 'user', user_message)
    
    # Build conversation context in Claude format
    messages = []
    for msg in reversed(chat_history):  # Oldest to newest
        messages.append({
            "role": msg['role'],
            "content": msg['content']
        })
        logger.debug(f"Added message: {msg['role']} - {msg['content'][:50]}...")

    # Add current user message
    messages.append({
        "role": "user", 
        "content": f"This is the current time, but you dont need to mention it in your response unless required: {current_time}\nUser message: {user_message}"
    })
    
    # Prepare request body for Claude streaming
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "temperature": temperature,
        "messages": messages,
        "system": system_prompts[0]["text"]
    }
    
    # Generate unique draft ID for this conversation
    draft_id = int(time.time() * 1000) % (2**31 - 1)  # Use timestamp as draft ID
    
    # Send initial draft message
    accumulated_text = "Thinking..."
    try:
        await context.bot.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            message_thread_id=message_thread_id,
            text=accumulated_text
        )
    except Exception as e:
        logger.warning(f"Failed to send initial draft: {e}")
        # Fallback to regular message if draft fails
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text="Processing your request..."
        )
    
    # Call Bedrock with streaming
    try:
        response = bedrock.invoke_model_with_response_stream(
            modelId=model_id,
            body=json.dumps(request_body)
        )
        
        # Process streaming response
        accumulated_text = ""
        stream = response.get('body')
        
        if stream:
            for event in stream:
                chunk = event.get('chunk')
                if chunk:
                    message = json.loads(chunk.get("bytes").decode())
                    
                    if message['type'] == "content_block_delta":
                        text_chunk = message['delta'].get('text', '')
                        if text_chunk:
                            accumulated_text += text_chunk
                            
                            # Update draft every few chunks to avoid rate limiting
                            if len(accumulated_text) % 100 < len(text_chunk):
                                try:
                                    await context.bot.send_message_draft(
                                        chat_id=chat_id,
                                        draft_id=draft_id,
                                        message_thread_id=message_thread_id,
                                        text=accumulated_text
                                    )
                                except Exception as e:
                                    logger.debug(f"Draft update failed: {e}")
                    
                    elif message['type'] == "message_stop":
                        logger.debug("Stream completed")
                        break
        
        # Send final complete message
        if accumulated_text:
            ptb_response_message = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text=accumulated_text
            )
            
            # Save bedrock response
            await save_message(chat_id, 'assistant', accumulated_text)
            
            logger.debug(f"Final response sent: {len(accumulated_text)} characters")
        else:
            # Fallback if no content received
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="I apologize, but I couldn't generate a response. Please try again."
            )
            return
    
    except Exception as e:
        logger.error(f"Error in streaming: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text="Sorry, I encountered an error processing your request. Please try again."
        )
        return

    # Check debug status before sending debug message
    debug_enabled = await get_debug_status(chat_id)
    logger.debug(f"Checking the debug status before sending debug messages: {debug_enabled}")
    if debug_enabled:
        await context.bot.send_message(
            chat_id=chat_id, 
            reply_to_message_id=ptb_response_message.message_id, 
            text=f"Debug: Bedrock streaming response completed - {len(accumulated_text)} characters"
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
    logger.info("Start processing document")
    
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
        logger.warning("File too large")
        # Send response to telegram
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"File is too large ({size_mb:.2f}MB). Maximum allowed size is 4.5MB. Please upload a smaller file.")
        return

    
    logger.debug(f"File name: {file_name}")
    logger.debug(f"MIME type: {mime_type}")
    logger.debug(f"File size: {file_size / (1024 * 1024):.2f}MB")
    

    
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
        logger.warning("Unsupported document type")
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
        
        logger.info(f"Successfully saved document as {file_type['type']}")

                # Sanitize the filename for Bedrock
        sanitized_name = sanitize_filename(file_name)
        logger.debug(f"Sanitized filename: {sanitized_name}")
        
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
                            },
                        "citations": {
                            "enabled": True
                        }
                        }
                    }
                ]
            }
        ]

        # Use the converse API with direct parameters
        response = bedrock.converse(
            modelId=model_id,
            messages=messages
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
        logger.debug(f"Checking the debug status before sending debug messages: {debug_enabled}")
        if debug_enabled:
            await context.bot.send_message(
                chat_id=chat_id, 
                reply_to_message_id=ptb_response_message.message_id, 
                text=f"Debug: \n Bedrock Response time: {bedrock_response_metrics / 1000} sec \n Bedrock Usage: {bedrock_response_usage}"
            )
        
    except Exception as e:
        error_message = f"Error processing document: {str(e)}"
        logger.error(error_message)
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

async def thinking_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Get current thinking status
    current_status = await get_thinking_status(chat_id)
    
    # Toggle the status
    new_status = not current_status
    
    # Save the new status
    if await save_thinking_status(chat_id, new_status):
        status_text = "enabled" if new_status else "disabled"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Chain of thought has been {status_text}."
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Failed to update thinking settings."
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
        logger.debug(f"Debug items: {items}")
        return items[0].get('debug_enabled', False) if items else False
    except Exception as e:
        logger.error(f"Error getting debug status: {e}")
        return False

async def save_debug_status(chat_id, status):
    try:
        current_time = datetime.utcnow()
        timestamp = current_time.isoformat()
        # Calculate TTL (current time + 1 hour) in epoch seconds
        ttl = int((current_time + timedelta(hours=1)).timestamp())
        
        table.put_item(Item={
            'chat_id': str(chat_id),
            'timestamp': timestamp,
            'record_type': 'DEBUG_SETTINGS',
            'debug_enabled': status,
            'expireat': ttl
        })
        return True
    except Exception as e:
        logger.error(f"Error saving debug status: {e}")
        return False

async def get_thinking_status(chat_id):
    try:
        response = table.query(
            IndexName='DebugSettingsIndex',
            KeyConditionExpression='chat_id = :chat_id AND record_type = :type',
            ExpressionAttributeValues={
                ':chat_id': str(chat_id),
                ':type': 'THINKING_SETTINGS'
            },
            ScanIndexForward=False,
            Limit=1
        )
        items = response.get('Items', [])
        logger.debug(f"Thinking items: {items}")
        return items[0].get('thinking_enabled', False) if items else False
    except Exception as e:
        logger.error(f"Error getting thinking status: {e}")
        return False

async def save_thinking_status(chat_id, status):
    try:
        current_time = datetime.utcnow()
        timestamp = current_time.isoformat()
        ttl = int((current_time + timedelta(hours=1)).timestamp())
        
        table.put_item(Item={
            'chat_id': str(chat_id),
            'timestamp': timestamp,
            'record_type': 'THINKING_SETTINGS',
            'thinking_enabled': status,
            'expireat': ttl
        })
        return True
    except Exception as e:
        logger.error(f"Error saving thinking status: {e}")
        return False



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
        logger.error(f"Error clearing chat history: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, I encountered an error while trying to clear your chat history."
        )

async def get_current_datetime():
    """Get current date and time in a formatted string"""
    current = datetime.utcnow()
    return current.strftime("%Y-%m-%d %H:%M:%S UTC")



async def main(event, context):
    # Register command handlers
    application.add_handler(CommandHandler('status', status_command))

    application.add_handler(CommandHandler('start', start_command))

    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), bedrock_stream))

    #Add the debug toggle command handler
    application.add_handler(CommandHandler('debug', debug_handler))
    
    # Add the thinking toggle command handler
    application.add_handler(CommandHandler('thinking', thinking_handler))

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
        logger.error("500 Exception")
        return {
            'statusCode': 500,
            'body': 'Failure'
        }

@bot.post("/bot")
async def webhook(request: Request):
    global start_time, lambda_context
    start_time = time.perf_counter()
    
    # Get headers
    headers = dict(request.headers)
    
    # Check if secret token header exists and matches expected value
    if 'x-telegram-bot-api-secret-token' not in headers or headers['x-telegram-bot-api-secret-token'] != TelegramBotAPISecretToken:
        logger.warning("Unauthorized - Telegram API Secret Token Header not found")
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    # Get request body
    body = await request.body()
    
    # Create mock event for compatibility with existing main function
    event = {"body": body.decode('utf-8'), "headers": headers}
    
    # Create mock context
    class MockContext:
        def __init__(self):
            self.function_version = "$LATEST"
        def get_remaining_time_in_millis(self):
            return 30000
    
    lambda_context = MockContext()
    
    result = await main(event, lambda_context)
    
    return JSONResponse(status_code=result.get('statusCode', 200), content={"message": result.get('body', 'Success')})

def lambda_handler(event, context):
    return event

