from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
import uvicorn
import json
import os
from typing import Dict, Any
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest

# ========== CONFIG ==========
CLIENT_SECRETS_FILE = "client_secret.json"  # provide by copying client_secret.json.example
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
]
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8000/oauth2callback")
TOKENS_DIR = "./tokens"
os.makedirs(TOKENS_DIR, exist_ok=True)
TOKEN_FILE = os.path.join(TOKENS_DIR, "user_token.json")

app = FastAPI(title="google-docs-mcp")


# ---------- OAuth routes for user to authorize ----------
@app.get("/")
async def index():
    return HTMLResponse("<a href='/authorize'>Authorize Google Docs access</a>")


@app.get("/authorize")
def authorize():
    """
    Returns a JSON response with the URL the developer/user should visit to grant consent.
    In production you might redirect the user directly to the URL.
    """
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", include_granted_scopes="true")
    return RedirectResponse(auth_url)



@app.get("/oauth2callback")
def oauth2callback(request: Request):
    """
    Exchange the code for tokens and save them locally.
    """
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(400, "No code in callback")
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    return HTMLResponse("Authorized. You can close this tab. Server ready.")


# ---------- Helper: load and refresh credentials ----------
def load_creds() -> Credentials:
    """
    Load Credentials from disk and refresh if needed.
    Raises HTTPException(401) if no saved credentials.
    """
    if not os.path.exists(TOKEN_FILE):
        raise HTTPException(401, "User not authorized. Visit /authorize")
    with open(TOKEN_FILE, "r") as f:
        creds_data = json.load(f)

    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)

    # If credentials are expired and a refresh token is available, refresh them
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            # On refresh failure, delete the token file to force re-authorization
            try:
                os.remove(TOKEN_FILE)
            except Exception:
                pass
            raise HTTPException(401, f"Failed to refresh credentials: {e}")

    return creds


# ---------- JSON-RPC style endpoint for MCP host ----------
@app.post("/mcp")
async def mcp_endpoint(req: Request):
    """
    Expects a JSON-RPC-like payload:
    { "method": "list_docs", "params": {...}, "id": "xyz" }
    Supported methods: list_docs, get_doc, insert_text, format_range
    """
    body = await req.json()
    method = body.get("method")
    params = body.get("params", {}) or {}
    request_id = body.get("id")

    try:
        if method == "list_docs":
            result = list_docs()
        elif method == "get_doc":
            document_id = params["documentId"]
            result = get_doc(document_id)
        elif method == "insert_text":
            result = insert_text(params["documentId"], params["text"], params.get("index", 1))
        elif method == "format_range":
            result = format_range(params["documentId"], params["start_index"], params["end_index"], params["format"])
        else:
            raise HTTPException(400, f"Unknown method {method}")

        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
    except KeyError as ke:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"message": f"missing param: {ke}"}} , status_code=400)
    except HTTPException as he:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"message": he.detail}}, status_code=he.status_code)
    except Exception as e:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"message": str(e)}}, status_code=500)


# ---------- Implementations using Google Docs API ----------
def list_docs() -> Any:
    creds = load_creds()
    drive = build("drive", "v3", credentials=creds)
    q = "mimeType='application/vnd.google-apps.document' and trashed=false"
    files = drive.files().list(q=q, pageSize=50, fields="files(id,name)").execute()
    return files.get("files", [])


def get_doc(document_id: str) -> Dict[str, Any]:
    creds = load_creds()
    docs = build("docs", "v1", credentials=creds)
    doc = docs.documents().get(documentId=document_id).execute()
    # The Documents API returns a complex body; returning title + body for MCP consumers
    return {"title": doc.get("title"), "body": doc.get("body")}


def insert_text(document_id: str, text: str, index: int = 1) -> Dict[str, Any]:
    creds = load_creds()
    docs = build("docs", "v1", credentials=creds)
    requests = [{"insertText": {"location": {"index": index}, "text": text}}]
    resp = docs.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
    return {"status": "ok", "updates": resp.get("replies")}


def format_range(document_id: str, start_index: int, end_index: int, format: str) -> Dict[str, Any]:
    """
    Supported format strings: "bold", "italic", "heading1"
    start_index and end_index should be integer indices in the document.
    """
    creds = load_creds()
    docs = build("docs", "v1", credentials=creds)

    if format == "bold":
        style = {"bold": True}
    elif format == "italic":
        style = {"italic": True}
    elif format == "heading1":
        # Apply a paragraph style named style
        requests = [
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start_index, "endIndex": end_index},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            }
        ]
        resp = docs.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
        return {"status": "ok", "updates": resp.get("replies")}
    else:
        raise Exception("unsupported format")

    # For text styles (bold/italic)
    requests = [
        {
            "updateTextStyle": {
                "range": {"startIndex": start_index, "endIndex": end_index},
                "textStyle": style,
                "fields": ",".join(style.keys()),
            }
        }
    ]
    resp = docs.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
    return {"status": "ok", "updates": resp.get("replies")}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
