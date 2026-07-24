import streamlit as st
import json
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

st.set_page_config(
    page_title="مساعد إدارة ومتابعة المشاريع - ME Assistant",
    page_icon="📊",
    layout="wide"
)

# 1. الاتصال بـ Google Drive عبر Service Account
@st.cache_resource
def get_drive_service():
    try:
        if "gcp_service_account" in st.secrets:
            secret_data = st.secrets["gcp_service_account"]
            if isinstance(secret_data, str):
                creds_info = json.loads(secret_data)
            else:
                creds_info = secret_data

            if "type" in creds_info and creds_info["type"] == "service_account":
                creds = service_account.Credentials.from_service_account_info(
                    creds_info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
                )
                return build("drive", "v3", credentials=creds)
            else:
                st.error("❌ الاعتمادات المرفقة ليست Service Account! يرجى وضع ملف مفتاح Service Account JSON الصحيح.")
                return None
        else:
            st.error("❌ لم يتم العثور على gcp_service_account في Secrets.")
            return None
    except Exception as e:
        st.error(f"❌ خطأ في الاتصال: {e}")
        return None

# 2. جلب الملفات والمجلدات
def list_files_recursive(service, folder_id, path_prefix=""):
    results = []
    query = f"'{folder_id}' in parents and trashed = false"
    
    try:
        response = service.files().list(
            q=query,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id, name, mimeType, webViewLink, modifiedTime)"
        ).execute()

        for item in response.get("files", []):
            current_path = f"{path_prefix}/{item['name']}"
            if item["mimeType"] == "application/vnd.google-apps.folder":
                results.extend(list_files_recursive(service, item["id"], current_path))
            else:
                results.append({
                    "اسم الملف": item["name"],
                    "المسار": current_path,
                    "رابط المعاينة": item.get("webViewLink", "#")
                })
    except Exception as e:
        st.error(f"حدث خطأ أثناء جلب الملفات من Google Drive: {e}")
        
    return results

# 3. الواجهة الرئيسية
st.title("📊 نظام إدارة ومتابعة المشاريع (ME Assistant)")
st.markdown("---")

if "projects" not in st.session_state:
    st.session_state.projects = {}

# الشريط الجانبي
with st.sidebar:
    st.header("⚙️ إدارة المشاريع")
    new_project_name = st.text_input("اسم المشروع")
    new_project_id = st.text_input("معرف المجلد (Folder ID)")
    
    if st.button("➕ إضافة المشروع"):
        if new_project_name and new_project_id:
            st.session_state.projects[new_project_name] = new_project_id.strip()
            st.success(f"تمت إضافة {new_project_name}")
        else:
            st.error("يرجى إدخال البيانات كاملة.")

service = get_drive_service()

if not st.session_state.projects:
    st.info("👈 أهلاً بك! قم بإضافة أول مشروع من الشريط الجانبي للبدء.")
else:
    project_names = list(st.session_state.projects.keys())
    selected_tab = st.tabs(project_names)

    for idx, name in enumerate(project_names):
        with selected_tab[idx]:
            folder_id = st.session_state.projects[name]
            st.subheader(f"📁 مستندات مشروع: {name}")
            
            if st.button(f"🔄 جلب/تحديث ملفات {name}", key=f"btn_{name}"):
                if service:
                    with st.spinner("جاري قراءة المجلدات المتفرعة..."):
                        files_data = list_files_recursive(service, folder_id)
                        if files_data:
                            st.success(f"تم العثور على {len(files_data)} ملف/ملفات.")
                            st.dataframe(
                                files_data,
                                column_config={
                                    "رابط المعاينة": st.column_config.LinkColumn("فتح الملف")
                                },
                                use_container_width=True
                            )
                        else:
                            st.warning("لم يتم العثور على ملفات. تأكد من مشاركة مجلد Drive مع بريد الـ Service Account.")
                else:
                    st.error("تعذر الاتصال بـ Google Drive. يرجى التحقق من إعدادات Secrets.")

# التوقيع والحقوق
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>", 
    unsafe_allow_html=True
)
