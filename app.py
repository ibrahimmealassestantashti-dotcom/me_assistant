import streamlit as st
import json
import os
import re
import io
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

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

def download_file_content(service, file_id, mime_type):
    """تنزيل محتوى الملف الفعلي من Google Drive ومعالجته"""
    try:
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

def extract_session_metrics_with_ai(service, session_info):
    """قراءة وفحص ملفات الحضور اليدوية PDF وتقارير Word بالذكاء الاصطناعي"""
    att_files = session_info.get("attendance", {}).get("files", [])
    rep_files = session_info.get("report", {}).get("files", [])
    
    att_text_content = ""
    rep_text_content = ""
    
    # محاولة جلب النصوص أو محتوى ملفات الحضور (PDF) والتقارير (Word)
    for f in att_files:
        content = download_file_content(service, f["id"], f.get("mimeType"))
        if content and "pdf" in f.get("name", "").lower():
            # إذا كان ملف PDF ممسوح ضوئياً، نعتمد على تحليله أو أسماء الملفات مع محتوى توضيحي
            att_text_content += f"[ملف حضور PDF باسم: {f['name']}] "
        elif content:
            att_text_content += f"[ملف حضور: {f['name']}] "
            
    for f in rep_files:
        content = download_file_content(service, f["id"], f.get("mimeType"))
        if content:
            # إذا كان ملف وورد (docx)، يمكننا تمرير اسمه على الأقل أو قراءه محتواه
            rep_text_content += f"[ملف تقرير Word باسم: {f['name']}] "

    prompt = f"""
    أنت مدقق بيانات مشاريع إنسانية وتنموية خبير في مطابقة المستندات.
    قم بتحليل بيانات الجلسة المعنونة باسم: "{session_info.get('session_name')}".
    معلومات الحضور المستخرجة من ورقة الحضور (المكتوبة بخط اليد مع استخدام حرف 'م' للحضور و 'غ' للغياب): {att_text_content}
    معلومات التقرير المكتوب بصيغة Word: {rep_text_content}
    
    مطلوب منك إخراج البيانات بصيغة JSON صارمة فقط وتحتوي على المفاتيح التالية:
    - attendance_data: [تاريخ الجلسة، العدد الإجمالي، الرجال (Men)، النساء (Women)، الأولاد (Boys)، الفتيات (Girls)، ذوي الاحتياجات (PWD)]
    - report_data: [تاريخ الجلسة، العدد الإجمالي، الرجال (Men)، النساء (Women)، الأولاد (Boys)، الفتيات (Girls)، ذوي الاحتياجات (PWD)]
    - differences: [قائمة تقييم لكل بند من البند السبعة: اكتب "✅ مطابق" أو "⚠️ خطأ / غير مطابق" بناءً على المقارنة]

    أجب بصيغة JSON فقط دون أي مقدمات أو نصوص خارجة عن كود الـ JSON.
    """
    
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        
        res = requests.post(url, json=payload, headers=headers, timeout=25)
        if res.status_code == 200:
            text_res = res.json()['candidates'][0]['content']['parts'][0]['text']
            cleaned = re.sub(r'```json|
