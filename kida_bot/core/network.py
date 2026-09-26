import urllib.request
import urllib.error
import time
import json
import logging
import random
import requests
import pathlib
import hashlib
import sys
import shutil
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum, auto
from collections import deque
from tenacity import retry, stop_after_attempt, wait_exponential

from kida_bot.config import logger, API_BASE

# Mock webhook for future integration
def send_webhook_alert(message: str, tier: str = "INFO"):
    logger.info(f"WEBHOOK [{tier}]: {message}")
    # TODO: Add requests.post(WEBHOOK_URL, json={"content": message}) here

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def http_get(url: str, timeout: int = 10):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def http_post(url: str, payload: dict, timeout: int = 35):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
