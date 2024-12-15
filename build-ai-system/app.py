import boto3
import json
import os
import requests
from supabase import create_client, Client
from datetime import datetime, timedelta
from PyPDF2 import PdfReader
import concurrent.futures

# CORS Headers
CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    'Access-Control-Allow-Methods': 'OPTIONS,POST,GET'
}

# Initialize Supabase client
SUPABASE_URL = "https://xsjzbkgsqtvlzyqeqbmx.supabase.co"
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY environment variable is not set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize the AWS Bedrock client for Claude 3
bedrock_client = boto3.client('bedrock-runtime', region_name='us-east-1')

url2="https://xsjzbkgsqtvlzyqeqbmx.supabase.co/storage/v1/object/public/ForLidar/Knowledge%20base%20/solvecrowd.pdf?t=2024-12-05T14%3A12%3A59.141Z"
temp_solvecrowd_path = "/tmp/solvecrowd.pdf"

url = "https://xsjzbkgsqtvlzyqeqbmx.supabase.co/storage/v1/object/public/ForLidar/Knowledge%20base%20/minohcSchdeule.pdf?t=2024-11-28T11%3A38%3A17.614Z"
temp_schedule_path = "/tmp/minohcschedule.pdf"

# Download PDF
def download_pdf(url, path):
    response = requests.get(url)
    if response.status_code == 200:
        with open(path, 'wb') as f:
            f.write(response.content)
        print(f"File downloaded successfully to {path}")
    else:
        print("Failed to download file.")

# Extract text from the PDF
def extract_text_from_pdf(pdf_path):
    with open(pdf_path, 'rb') as file:
        reader = PdfReader(file)
        text = ""
        for page in reader.pages:
            text += page.extract_text()
    return text

download_pdf(url, temp_schedule_path)
download_pdf(url2, temp_solvecrowd_path)

pdf_text = extract_text_from_pdf(temp_schedule_path)
pdf_text2 = extract_text_from_pdf(temp_solvecrowd_path)

# Fetch data concurrently 
def fetch_all_data():
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {
            'current_data': executor.submit(fetch_current_data),
            'last_week_data': executor.submit(fetch_last_week_data),
            'suspicious_data': executor.submit(fetch_suspicious_data),
            'project_times': executor.submit(fetch_start_last_times),
            'max_min': executor.submit(fetch_get_max_min_data),
            'interval_data': executor.submit(fetch_data_for_interval),
            'weather_times': executor.submit(fetch_weather_data_for_next_days),
            'zone_data': executor.submit(fetch_thirdFloor_zone),
        }

        # Collect the results
        results = {key: future.result() for key, future in futures.items()}
        return results

# Fetch the required data from Supabase
def fetch_current_data():
    return fetch_data_from_supabase('get_current_time_data')

def fetch_last_week_data():
    return fetch_data_from_supabase('get_last_week_data')

def fetch_suspicious_data():
    return fetch_data_from_supabase('get_find_suspicious')

def fetch_start_last_times():
    return fetch_data_from_supabase('get_start_time_and_last_time')

def fetch_get_max_min_data():
    return fetch_data_from_supabase('get_max_min_data')

def fetch_data_for_interval():
    return fetch_data_from_supabase('get_thirdfloor_hourdata', {'hours_interval': 1})

def fetch_weather_data_for_next_days():
    return fetch_data_from_supabase('get_weather_data_for_next_days', {'num_days': 1})

def fetch_thirdFloor_zone():
    return fetch_data_from_supabase('get_thirdfloor_zones')

def fetch_data_from_supabase(function_name, params=None):
    try:
        data = supabase.rpc(function_name, params).execute()
        if data.data:
            return data.data
        return None
    except Exception as e:
        print(f"Error fetching data: {str(e)}")
        return None

# Generate the prompt for Bedrock
def get_answer_from_claude(question, data):
    try:
        context = {
            'current_data': "\n".join([f"• Time: {entry['time']} - Number of People: {entry['num']}" for entry in data['current_data']]),
            'last_week_data': "\n".join([f"• Time: {entry['time']} - Number of People: {entry['num']}" for entry in data['last_week_data']]),
            'suspicious_data': "\n".join([f"• Time: {entry['event_time']} - Number of People: {entry['num']}" for entry in data['suspicious_data']]),
            'project_times': "\n".join([f"• Start Time: {entry['start_time']} - End Time: {entry['last_time']}" for entry in data['project_times']]),
            'max_min_data': "\n".join([f"• Max: {entry.get('max_num', 'N/A')} at {entry.get('max_time', 'N/A')}, Min: {entry.get('min_num', 'N/A')} at {entry.get('min_time', 'N/A')}" for entry in data['max_min']]),
            'interval_data': "\n".join([f"• Time: {entry['time']} - People: {entry['num']}" for entry in data['interval_data']]),
            'weather_data': "\n".join([f"• Time: {entry['weather_time']} - Temperature: {entry['temperature_2m_celsius']}" for entry in data['weather_times']]),
            'zone_data': "\n".join([f"• Zone: {entry['zone_name']} - Capacity: {entry['capacity']}" for entry in data['zone_data']]),
            'pdf_text': pdf_text,
            'pdf_text2': pdf_text2,
        }

        prompt = f"""
        あなたは建物の利用状況を分析するアシスタントです。以下のデータを基に、ユーザーの質問に正確に答えてください。
        注意点は予測と関係ある質問のみinterval_data、weather_data、zone_data、pdf_text, last_week_data のデータを使ってください。
        食堂混雑についてアドバイスするためにcontext_pdf2データを利用しください。
        質問: {question}
        利用可能なデータ: 
        {context['current_data']}
        {context['last_week_data']}
        {context['suspicious_data']}
        {context['project_times']}
        {context['max_min_data']}
        {context['interval_data']}
        {context['weather_data']}
        {context['zone_data']}
        {context['pdf_text']}
        {context['pdf_text2']}   

        """
        
        input_data = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
            "anthropic_version": "bedrock-2023-05-31"
        }

        response = bedrock_client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps(input_data),
            contentType='application/json'
        )

        response_body = json.loads(response['body'].read().decode('utf-8'))
        return response_body.get('content', "Sorry, I couldn't process your question.")
    except Exception as e:
        print(f"Error querying Bedrock: {str(e)}")
        return "Sorry, there was an error processing your question."

# Lambda function handler
def lambda_handler(event, context):
    http_method = event.get('httpMethod', None)

    if http_method == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': CORS_HEADERS,
            'body': json.dumps({'message': 'CORS Preflight Successful'})
        }

    body = event.get('body', '{}')
    try:
        body = json.loads(body)
    except json.JSONDecodeError:
        return {
            'statusCode': 400,
            'headers': CORS_HEADERS,
            'body': json.dumps({'error': 'Invalid JSON in request body.'})
        }

    user_question = body.get('question', '').strip()
    if not user_question:
        return {
            'statusCode': 400,
            'headers': CORS_HEADERS,
            'body': json.dumps({'message': 'No question provided.'})
        }

    # Fetch data concurrently
    data = fetch_all_data()
    if not any(data.values()):
        return {
            'statusCode': 404,
            'headers': CORS_HEADERS,
            'body': json.dumps({'message': 'No data found.'})
        }

    answer = get_answer_from_claude(user_question, data)

    return {
        'statusCode': 200,
        'headers': CORS_HEADERS,
        'body': json.dumps({
            'question': user_question,
            'response': answer
        })
    }
