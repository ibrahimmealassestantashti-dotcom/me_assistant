import streamlit as st
import json
import os
import time
import io
import tempfile
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai
from google.genai.errors import ClientError

st.set_page_config(
    page_title="مساعد إدارة ومتابعة المشاريع - ME Assistant",
    page_icon="📊",
    layout="wide"
)

CONFIG_FILE = "saved_projects.json"
LOGS_FILE = "scan_logs.json"

def load_saved_projects():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_projects(projects_dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(projects_dict, f, ensure_ascii=False, indent=4)
    except Exception as e:
        st.error(f"خطأ أثناء حفظ البيانات: {e}")

def load_scan_logs():
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_scan_logs(logs_dict):
    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(logs_dict, f, ensure_ascii=False, indent=4)
    except Exception as e:
        pass

@st.cache_resource
def get_drive_service():
    try:
        if "gcp_service_account" in st.secrets:
            creds_data = dict(st.secrets["gcp_service_account"])
            if "private_key" in creds_data:
                creds_data["private_key"] = creds_data["private_key"].replace("\\n", "\n")

            creds = service_account.Credentials.from_service_account_info(
                creds_data, scopes=["https://www.googleapis.com/auth/drive.readonly"]
            )
            return build("drive", "v3", credentials=creds)
        else:
            st.error("❌ لم يتم العثور على gcp_service_account في Secrets.")
            return None
    except Exception as e:
        st.error(f"❌ خطأ في الاتصال: {e}")
        return None

def get_folder_contents(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    folders = []
    files = []
    try:
        results = service.files().list(
            q=query, supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="files(id, name, mimeType, webViewLink)"
        ).execute()
        
        for item in results.get("files", []):
            if item["mimeType"] == "application/vnd.google-apps.folder":
                folders.append(item)
            else:
                files.append(item)
    except Exception as e:
        st.error(f"خطأ أثناء قراءة المجلد: {e}")
    return folders, files

def is_sub_component(folder_name):
    name = folder_name.lower().strip()
    keywords = [
        "attendance", "attend", "حضور", "كشف", "اسماء", "أسماء",
        "documentation", "doc", "photo", "photos", "image", "images", "صور", "توثيق", "أرشيف",
        "report", "reports", "تقرير", "تقارير", "ملخص"
    ]
    return any(kw in name for kw in keywords)

def parse_session_subfolders(service, session_folder):
    sub_folders, direct_files = get_folder_contents(service, session_folder["id"])
    
    session_data = {
        "session_id": session_folder["id"],
        "session_name": session_folder["name"],
        "attendance": {"folder": None, "files": []},
        "documentation": {"folder": None, "files": []},
        "report": {"folder": None, "files": []},
        "extra_files": direct_files
    }
    
    for sf in sub_folders:
        sf_name_lower = sf["name"].lower().strip()
        _, files = get_folder_contents(service, sf["id"])
        
        if any(k in sf_name_lower for k in ["attendance", "attend", "حضور", "كشف", "اسماء", "أسماء"]):
            session_data["attendance"]["folder"] = sf
            session_data["attendance"]["files"] = files
        elif any(k in sf_name_lower for k in ["documentation", "doc", "photo", "photos", "image", "images", "صور", "توثيق", "أرشيف"]):
            session_data["documentation"]["folder"] = sf
            session_data["documentation"]["files"] = files
        elif any(k in sf_name_lower for k in ["report", "reports", "تقرير", "تقارير", "ملخص"]):
            session_data["report"]["folder"] = sf
            session_data["report"]["files"] = files
            
    return session_data

def fetch_structured_sessions(service, target_folder_id, base_path_str=""):
    sessions_list = []
    top_folders, _ = get_folder_contents(service, target_folder_id)
    
    for folder in top_folders:
        if is_sub_component(folder["name"]):
            continue
            
        child_folders, _ = get_folder_contents(service, folder["id"])
        has_sub_components = any(is_sub_component(cf["name"]) for cf in child_folders)
        
        if has_sub_components:
            parsed = parse_session_subfolders(service, folder)
            if base_path_str:
                parsed["session_name"] = f"{base_path_str} ➔ {folder['name']}"
            else:
                parsed["session_name"] = folder['name']
            sessions_list.append(parsed)
        else:
            for cf in child_folders:
                if is_sub_component(cf["name"]):
                    continue
                    
                sub_cf_folders, _ = get_folder_contents(service, cf["id"])
                if any(is_sub_component(scf["name"]) for scf in sub_cf_folders):
                    parsed = parse_session_subfolders(service, cf)
                    if base_path_str:
                        parsed["session_name"] = f"{base_path_str} ➔ {folder['name']} ➔ {cf['name']}"
                    else:
                        parsed["session_name"] = f"{folder['name']} ➔ {cf['name']}"
                    sessions_list.append(parsed)
                    
    return sessions_list

def get_file_content_bytes(service, file_id, mime_type):
    try:
        if mime_type == 'application/vnd.google-apps.document':
            request = service.files().export_media(fileId=file_id, mimeType='text/plain')
        elif mime_type == 'application/vnd.google-apps.spreadsheet':
            request = service.files().export_media(fileId=file_id, mimeType='text/csv')
        else:
            request = service.files().get_media(fileId=file_id)
        
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception:
        return None

def extract_number(text):
    if not text:
        return 0
    match = re.search(r'\d+', str(text))
    return int(match.group()) if match else 0

def analyze_session_files_with_ai(service, session_info, api_key):
    if "cached_metrics" in session_info:
        return session_info["cached_metrics"]
        
    att_files = session_info.get("attendance", {}).get("files", [])
    rep_files = session_info.get("report", {}).get("files", [])
    
    error_result = (
        ["❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر"],
        ["❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر", "❌ غير متوفر"],
        ["❌ ملفات الحضور أو التقرير مفقودة", "❌ خطأ", "❌ خطأ", "❌ خطأ", "❌ خطأ", "❌ خطأ", "❌ خطأ"]
    )

    if not api_key or (not att_files and not rep_files):
        session_info["cached_metrics"] = error_result
        return error_result

    client = genai.Client(api_key=api_key.strip())
    
    # آلية إعادة المحاولة عند تجاوز حد API (Rate Limit / 429)
    max_retries = 3
    for attempt in range(max_retries):
        uploaded_gemini_files = []
        try:
            for f in att_files:
                b = get_file_content_bytes(service, f["id"], f["mimeType"])
                if b:
                    suffix = ".pdf" if "pdf" in f["mimeType"] else (".png" if "image" in f["mimeType"] else ".txt")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        tmp.write(b)
                        tmp_path = tmp.name
                    up_file = client.files.upload(file=tmp_path)
                    uploaded_gemini_files.append(up_file)
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass

            for f in rep_files:
                b = get_file_content_bytes(service, f["id"], f["mimeType"])
                if b:
                    suffix = ".pdf" if "pdf" in f["mimeType"] else (".docx" if "document" in f["mimeType"] else ".txt")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        tmp.write(b)
                        tmp_path = tmp.name
                    up_file = client.files.upload(file=tmp_path)
                    uploaded_gemini_files.append(up_file)
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass

            if not uploaded_gemini_files:
                session_info["cached_metrics"] = error_result
                return error_result

            prompt = [
                "أنت مدقق ومراجع دقيق لمستندات المشاريع والمخيمات.",
                "قم بقراءة محتوى الملفات المرفقة فعلياً (أوراق الحضور وتقارير الإنجاز) واستخرج القيم الحقيقية بدقة تامة.",
                "تحذير صارم: يمنع منعاً باتاً افتراض أو تخمين أي أرقام أو وضع أصفار إذا كانت البيانات ناقصة أو غير واضحة.",
                "أجب بصيغة JSON صارمة فقط بالشكل التالي ودون أي نصوص إضافية:",
                "{\n"
                '  "attendance_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
                '  "report_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
                '  "differences": ["النتيجة للبند 1", "النتيجة للبند 2", "النتيجة للبند 3", "النتيجة للبند 4", "النتيجة للبند 5", "النتيجة للبند 6", "النتيجة للبند 7"]\n'
                "}"
            ]
            for uf in uploaded_gemini_files:
                prompt.append(uf)

            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            
            text_res = response.text
            cleaned = text_res.replace("```json", "").replace("
