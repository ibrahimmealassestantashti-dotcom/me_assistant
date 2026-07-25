import streamlit as st
import json
import os
import re
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

st.set_page_config(
    page_title="مساعد إدارة ومتابعة المشاريع - ME Assistant",
    page_icon="📊",
    layout="wide"
)

CONFIG_FILE = "saved_projects.json"

# مفتاح الذكاء الاصطناعي الخاص بك
GEMINI_API_KEY = "AQ.Ab8RN6KgF9CANRPsP--41d3hJGfWpEb8a9M9nwI6Ely_6qXv_Q"

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

def fetch_structured_sessions(service, target_folder_id):
    sessions_list = []
    top_folders, _ = get_folder_contents(service, target_folder_id)
    
    for folder in top_folders:
        if is_sub_component(folder["name"]):
            continue
            
        child_folders, _ = get_folder_contents(service, folder["id"])
        has_sub_components = any(is_sub_component(cf["name"]) for cf in child_folders)
        
        if has_sub_components:
            parsed = parse_session_subfolders(service, folder)
            sessions_list.append(parsed)
        else:
            for cf in child_folders:
                if is_sub_component(cf["name"]):
                    continue
                    
                sub_cf_folders, _ = get_folder_contents(service, cf["id"])
                if any(is_sub_component(scf["name"]) for scf in sub_cf_folders):
                    parsed = parse_session_subfolders(service, cf)
                    parsed["session_name"] = f"{folder['name']} / {cf['name']}"
                    sessions_list.append(parsed)
                    
    return sessions_list

def extract_session_metrics_with_ai(session_info):
    """استخدام الذكاء الاصطناعي لتحليل أسماء ومحتويات الملفات واستخراج القيم الحقيقية"""
    att_files = session_info.get("attendance", {}).get("files", [])
    rep_files = session_info.get("report", {}).get("files", [])
    
    att_names = [f["name"] for f in att_files]
    rep_names = [f["name"] for f in rep_files]
    
    prompt = f"""
    أنت مدقق بيانات مشاريع. بناءً على أسماء ملفات الحضور وملفات التقارير التالية لجلسة "{session_info.get('session_name')}", قم بتقدير أو استخراج القيم الحقيقية بدقة على شكل صيغة JSON فقط تتضمن مفاتيح محددة:
    - attendance_data: [التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات]
    - report_data: [التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات]
    - differences: [قائمة تقييم لكل بند مطابقة أو خطأ]

    أسماء ملفات الحضور: {att_names}
    أسماء ملفات التقارير: {rep_names}
    
    أجب بصيغة JSON صارمة فقط دون أي نصوص إضافية، تحتوي على المصفوفات السابقة بالترتيب.
    """
    
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        
        res = requests.post(url, json=payload, headers=headers, timeout=20)
        if res.status_code == 200:
            text_res = res.json()['candidates'][0]['content']['parts'][0]['text']
            cleaned = re.sub(r'```json|
