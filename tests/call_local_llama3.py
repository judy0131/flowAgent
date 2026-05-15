import requests

url = "http://localhost:11434/api/generate"

data = {
    "model": "llama3",
    "prompt": "introduce trusted data space",
    "stream": False
}

response = requests.post(url, json=data)
print(response.json()["response"])