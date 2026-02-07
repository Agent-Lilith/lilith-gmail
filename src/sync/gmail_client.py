from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from typing import List, Dict, Any, Optional
import base64
from bs4 import BeautifulSoup

from core.preprocessing import strip_tracking_pixels_from_html

class GmailClient:
    def __init__(self, credentials: Credentials):
        self.service = build('gmail', 'v1', credentials=credentials)
        self.user_id = 'me'

    def setup_push_notifications(self, topic_name: str, label_ids: List[str] = None):
        if label_ids is None:
            label_ids = ['INBOX']
            
        request = {
            'labelIds': label_ids,
            'topicName': topic_name
        }
        return self.service.users().watch(
            userId=self.user_id,
            body=request
        ).execute()

    def stop_notifications(self):
        return self.service.users().stop(userId=self.user_id).execute()

    def list_messages(self, query: str = None, max_results: int = 100, page_token: str = None):
        return self.service.users().messages().list(
            userId=self.user_id,
            q=query,
            maxResults=max_results,
            pageToken=page_token
        ).execute()

    def get_message(self, message_id: str, format: str = 'full'):
        return self.service.users().messages().get(
            userId=self.user_id,
            id=message_id,
            format=format
        ).execute()

    def get_profile(self):
        return self.service.users().getProfile(userId=self.user_id).execute()

    def list_labels(self) -> List[Dict[str, Any]]:
        response = self.service.users().labels().list(userId=self.user_id).execute()
        return response.get("labels", [])

    def get_history(self, start_history_id: int, page_token: str | None = None):
        params = {
            "userId": self.user_id,
            "startHistoryId": start_history_id,
        }
        if page_token:
            params["pageToken"] = page_token
        return self.service.users().history().list(**params).execute()

    def get_attachment(self, message_id: str, attachment_id: str):
        response = self.service.users().messages().attachments().get(
            userId=self.user_id,
            messageId=message_id,
            id=attachment_id
        ).execute()
        return base64.urlsafe_b64decode(response.get('data', ''))

    @staticmethod
    def parse_body(payload: Dict[str, Any]) -> str:
        if 'parts' in payload:
            text_part = None
            html_part = None
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    text_part = part
                elif part['mimeType'] == 'text/html':
                    html_part = part
                elif 'parts' in part:  # multipart/alternative etc.
                    nested_body = GmailClient.parse_body(part)
                    if nested_body:
                        return nested_body
            
            if text_part and text_part['body'].get('data'):
                return base64.urlsafe_b64decode(text_part['body']['data']).decode('utf-8')
            if html_part and html_part['body'].get('data'):
                html = base64.urlsafe_b64decode(html_part['body']['data']).decode('utf-8')
                html = strip_tracking_pixels_from_html(html)
                return BeautifulSoup(html, 'html.parser').get_text(separator=' ', strip=True)
        
        if 'body' in payload and payload['body'].get('data'):
            data = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
            if payload.get('mimeType') == 'text/html':
                data = strip_tracking_pixels_from_html(data)
                return BeautifulSoup(data, 'html.parser').get_text(separator=' ', strip=True)
            return data
            
        return ""
