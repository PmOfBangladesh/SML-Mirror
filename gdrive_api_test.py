import io
import pickle
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth.transport.requests import Request

FILE_ID = "1yKv7VmBtCrEX4VxzgjdCjT2jPaZY5eC7"
OUT = "api_download_test"

with open("token.pickle", "rb") as f:
    creds = pickle.load(f)

if creds.expired and creds.refresh_token:
    creds.refresh(Request())
    with open("token.pickle", "wb") as f:
        pickle.dump(creds, f)

service = build("drive", "v3", credentials=creds)

meta = service.files().get(
    fileId=FILE_ID,
    fields="id,name,mimeType,size"
).execute()
print("FILE:", meta)

request = service.files().get_media(fileId=FILE_ID)
fh = io.FileIO(OUT, "wb")
downloader = MediaIoBaseDownload(fh, request)

done = False
while not done:
    status, done = downloader.next_chunk()
    if status:
        print(f"Download {int(status.progress() * 100)}%")

print("Saved:", OUT)
