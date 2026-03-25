#!/usr/bin/env python3
import json
import time
import urllib.request
import urllib.error
import os
from datetime import datetime

# Targeting the CPU-only Ollama container on port 11435
MODELS = ["llama3.2:latest", "qwen3.5:4b", "qwen3.5:9b"]
OLLAMA_URL = "http://localhost:11435/api/generate"

SAMPLES = [
    {
        "id": "1",
        "from": "Delta Air Lines <delta@delta.com>",
        "subject": "Your flight DL123 is delayed 45 minutes",
        "body": "Hi Dan, your flight from JFK to LAX is delayed. New departure time is 4:45 PM. Safe travels!"
    },
    {
        "id": "2",
        "from": "Substack <hello@substack.com>",
        "subject": "The Weekly Roundup: Why AI is tiny now",
        "body": "In this week's newsletter, we dive deep into the world of small language models and how they are changing everything..."
    },
    {
        "id": "3",
        "from": "LinkedIn <messages-noreply@linkedin.com>",
        "subject": "15 people viewed your profile today",
        "body": "See who's looking at your profile and grow your professional network. Unlock more features with Premium."
    },
    {
        "id": "4",
        "from": "Chase Online <alerts@chase.com>",
        "subject": "Large transaction alert on your account",
        "body": "A transaction of \$1,200.00 was authorized at Apple Store on March 08. If this was not you, call us immediately."
    }
]

def infer(model, prompt):
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 50, "temperature": 0}
    }).encode('utf-8')
    
    start_time = time.time()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            content = r.read().decode('utf-8')
            resp = json.loads(content)
            latency = time.time() - start_time
            return resp.get("response", "").strip(), latency
    except Exception as e:
        return f"ERROR: {e}", time.time() - start_time

def run_test():
    print(f"--- Triage Comparison Run ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ---")
    print(f"{'Model':<15} | {'Verdict':<10} | {'Lat (s)':<7} | {'Subject'}")
    print("-" * 90)
    
    for sample in SAMPLES:
        prompt = (
            f"Email Triage Task.\n"
            f"From: {sample['from']}\n"
            f"Subject: {sample['subject']}\n"
            f"Body: {sample['body']}\n\n"
            "Classify this email. Answer with exactly one of these words:\n"
            "URGENT - travel alerts, security, direct human messages\n"
            "DIGEST - newsletters, summary updates\n"
            "NOISE - social notifications, ads\n\n"
            "Verdict:"
        )
        
        for model in MODELS:
            raw_response, latency = infer(model, prompt)
            v_clean = "N/A"
            if raw_response:
                if raw_response.startswith("ERROR"):
                    v_clean = "ERR"
                else:
                    words = raw_response.upper().replace('.', '').replace(':', '').replace('*', '').split()
                    for word in ["URGENT", "DIGEST", "NOISE"]:
                        if word in words:
                            v_clean = word
                            break
                    if v_clean == "N/A" and words:
                        v_clean = words[0][:10]
            
            # Debug: if verdict is N/A or we want to see nuance
            debug_info = f" | Raw: {raw_response[:20]}..." if v_clean == "N/A" else ""
            print(f"{model:<15} | {v_clean:<10} | {latency:<7.2f} | {sample['subject'][:40]}...{debug_info}")

if __name__ == '__main__':
    run_test()
