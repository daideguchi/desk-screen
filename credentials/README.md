# credentials/

ここには **ローカル専用の機密ファイル**（OAuthトークンやサービスアカウントJSON）を置きます。

- `service_account.json`（任意）: Google Calendar Service Account
- `user_oauth_token.json`（任意）: `setup_google_oauth.py` で作られる OAuth トークン

`.gitignore` で `credentials/*.json` はコミットされません。
