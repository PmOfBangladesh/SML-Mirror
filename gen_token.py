from google_auth_oauthlib.flow import InstalledAppFlow
import pickle

SCOPES = ['https://www.googleapis.com/auth/drive']

flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(host='0.0.0.0', port=53999, open_browser=False)

with open('token.pickle', 'wb') as f:
    pickle.dump(creds, f)

print("DONE")