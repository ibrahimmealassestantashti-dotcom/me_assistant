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
    except Exception as e:
        st.error(f"خطأ في تحميل ملف Drive (ID: {file_id}): {e}")
        return None

def extract_number(text):
    if not text:
        return 0
    match = re.search(r'\d+', str(text))
    return int(match.group()) if match else 0

def call_gemini_with_retry(client, model, contents, max_retries=3, initial_delay=3):
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(initial_delay * (2 ** attempt))
                continue
            raise e

def analyze_session_files_with_ai(service, session_info, api_key, force_refresh=False):
    if not force_refresh and "cached_metrics" in session_info:
        return session_info["cached_metrics"]
        
    att_files = session_info.get("attendance", {}).get("files", [])
    rep_files = session_info.get("report", {}).get("files", [])
    
    if not api_key:
        st.error("❌ مفتاح Gemini API غير موجود.")
        return None

    if not att_files and not rep_files:
        st.warning(f"⚠️ الجلسة '{session_info.get('session_name')}' لا تحتوي على ملفات حضور أو تقارير.")
        empty_res = (
            ["لا توجد ملفات", "-", "-", "-", "-", "-", "-"],
            ["لا توجد ملفات", "-", "-", "-", "-", "-", "-"],
            ["الملفات مفقودة بالمجلد", "-", "-", "-", "-", "-", "-"]
        )
        session_info["cached_metrics"] = empty_res
        return empty_res

    client = genai.Client(api_key=api_key.strip())
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
            st.error("❌ تعذر رفع أي ملف إلى خادم الذكاء الاصطناعي.")
            return None

        prompt = [
            "أنت مدقق ومراجع دقيق لمستندات المشاريع والمخيمات.",
            "قم بقراءة محتوى الملفات المرفقة فعلياً (أوراق الحضور وتقارير الإنجاز) واستخرج القيم الحقيقية بدقة تامة.",
            "تحذير صارم: يمنع منعاً باتاً افتراض أو تخمين أي أرقام أو وضع أصفار إذا كانت البيانات ناقصة أو غير واضحة. إذا وجد نقص أو تعذر القراءة، يجب كتابة عبارات صريحة تدل على الخطأ مثل '❌ غير واضح'.",
            "قم بمقارنة أوراق الحضور مع التقرير لكل بند بدقة تامة واكتب نتيجة المقارنة.",
            "أجب بصيغة JSON صارمة فقط بالشكل التالي ودون أي نصوص إضافية أو علامات تMarkdown خارجية خارج الكود:",
            "{\n"
            '  "attendance_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
            '  "report_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
            '  "differences": ["النتيجة للبند 1", "النتيجة للبند 2", "النتيجة للبند 3", "النتيجة للبند 4", "النتيجة للبند 5", "النتيجة للبند 6", "النتيجة للبند 7"]\n'
            "}"
        ]
        for uf in uploaded_gemini_files:
            prompt.append(uf)

        # استبدال النموذج بنموذج مستقر
        response = call_gemini_with_retry(
            client=client,
            model="gemini-2.5-flash",
            contents=prompt
        )
        
        text_res = response.text
        cleaned = text_res.replace("```json", "").replace("```", "").strip()
        data = json.loads(cleaned)
        
        result = (
            data.get("attendance_data", []),
            data.get("report_data", []),
            data.get("differences", [])
        )
        session_info["cached_metrics"] = result
        return result

    except Exception as e:
        # عرض الخطأ الحقيقي بوضوح على الشاشة لمعرفة سبب الفشل بدقة
        st.error(f"❌ حدث خطأ أثناء تحليل الجلسة '{session_info.get('session_name')}':\n`{str(e)}`")
        err_tuple = (
            ["خطأ بالتحليل", "-", "-", "-", "-", "-", "-"],
            ["خطأ بالتحليل", "-", "-", "-", "-", "-", "-"],
            [f"خطأ: {str(e)[:100]}", "-", "-", "-", "-", "-", "-"]
        )
        session_info["cached_metrics"] = err_tuple
        return err_tuple
    finally:
        for uf in uploaded_gemini_files:
            try:
                client.files.delete(name=uf.name)
            except:
                pass

# --- الواجهة الرئيسية ---
st.title("📊 نظام إدارة ومتابعة المشاريع الذكي (ME Assistant)")
st.markdown("---")

if "projects" not in st.session_state:
    st.session_state.projects = load_saved_projects()

with st.sidebar:
    st.header("⚙️ إعدادات النظام والمشاريع")
    
    default_api_key = st.secrets.get("GEMINI_API_KEY", "")
    GEMINI_API_KEY = st.text_input("مفتاح Gemini API أو رمز المصادقة:", value=default_api_key, type="password")
    
    st.markdown("---")
    st.subheader("إضافة مشروع جديد")
    new_project_name = st.text_input("اسم المشروع")
    new_project_id = st.text_input("معرف المجلد (Folder ID)")
    
    if st.button("➕ إضافة وحفظ المشروع"):
        if new_project_name and new_project_id:
            st.session_state.projects[new_project_name] = new_project_id.strip()
            save_projects(st.session_state.projects)
            st.success(f"تم حفظ مشروع '{new_project_name}' بنجاح!")
            st.rerun()
        else:
            st.error("يرجى إدخال البيانات كاملة.")
            
    if st.session_state.projects:
        st.markdown("---")
        st.subheader("📋 المشاريع المسجلة:")
        for p_name in list(st.session_state.projects.keys()):
            col_p1, col_p2 = st.columns([3, 1])
            col_p1.text(p_name)
            if col_p2.button("🗑️", key=f"del_{p_name}"):
                del st.session_state.projects[p_name]
                save_projects(st.session_state.projects)
                st.rerun()

service = get_drive_service()

if not GEMINI_API_KEY:
    st.warning("⚠️ الرجاء إدخال مفتاح الذكاء الاصطناعي في الشريط الجانبي لتفعيل التحليل وقراءة محتوى الملفات.")

if not st.session_state.projects:
    st.info("👈 قم بإضافة أول مشروع من الشريط الجانبي وسيبقى محفوظاً دائماً.")
else:
    project_names = list(st.session_state.projects.keys())
    selected_tab = st.tabs(project_names)

    for idx, p_name in enumerate(project_names):
        with selected_tab[idx]:
            root_id = st.session_state.projects[p_name]
            
            path_key = f"path_{p_name}"
            if path_key not in st.session_state:
                st.session_state[path_key] = [{"id": root_id, "name": p_name}]
                
            current_trail = st.session_state[path_key]
            current_folder = current_trail[-1]
            
            st.subheader(f"📁 متصفح مجلدات مشروع: {p_name}")
            trail_str = " ➔ ".join([node["name"] for node in current_trail])
            st.info(f"📍 **المسار الحالي:** {trail_str}")
            
            col_nav1, _ = st.columns([1, 4])
            with col_nav1:
                if len(current_trail) > 1:
                    if st.button("⬅️ العودة للمجلد السابق", key=f"back_{p_name}"):
                        st.session_state[path_key].pop()
                        st.rerun()
                        
            sub_folders, direct_files = get_folder_contents(service, current_folder["id"])
            
            if sub_folders:
                folder_options = {sf["name"]: sf["id"] for sf in sub_folders}
                selected_sub_name = st.selectbox(
                    "اختر المجلد/النشاط للانتقال إليه:",
                    ["-- اختر المجلد --"] + list(folder_options.keys()),
                    key=f"select_folder_{p_name}"
                )
                
                if selected_sub_name != "-- اختر المجلد --":
                    next_id = folder_options[selected_sub_name]
                    st.session_state[path_key].append({"id": next_id, "name": selected_sub_name})
                    st.rerun()

            st.markdown("---")
            
            btn_fetch = st.button(
                f"⚡ جلب هيكلية الجلسات تحت ({current_folder['name']})", 
                key=f"fetch_btn_{p_name}",
                type="primary"
            )

            if btn_fetch:
                with st.spinner("جاري جلب هيكلية الجلسات والمجلدات..."):
                    base_path_str = " ➔ ".join([node["name"] for node in current_trail])
                    sessions = fetch_structured_sessions(service, current_folder["id"], base_path_str)
                    st.session_state[f"data_{p_name}"] = sessions

            sessions_data = st.session_state.get(f"data_{p_name}", None)
            
            if sessions_data is not None:
                if not sessions_data:
                    st.warning("لم يتم العثور على جلسات مكتملة الهيكلية داخل هذا المجلد.")
                else:
                    st.success(f"تم اكتشاف {len(sessions_data)} جلسة/جلسات بالهيكلية الصحيحة!")
                    st.markdown("---")
                    
                    st.subheader("🛠️ أدوات التحليل والمطابقة الذكية")
                    b1, b2, b3, b4, b5, b6 = st.columns(6)
                    
                    if b1.button("1️⃣ فحص المرفقات", key=f"b1_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "attachments"
                    if b2.button("2️⃣ مطابقة الحضور", key=f"b2_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "matching"
                    if b3.button("3️⃣ إحصائية الفئات", key=f"b3_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "stats"
                    if b4.button("4️⃣ سجل الفحص 📋", key=f"b4_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "logs"
                    if b5.button("5️⃣ تقرير الفجوات 📊", key=f"b5_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "gap_analysis"
                    if b6.button("6️⃣ المساعد الذكي 💬", key=f"b6_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "chat"

                    current_view = st.session_state.get(f"view_{p_name}", None)

                    if current_view == "attachments":
                        st.markdown("#### 📋 نتيجة فحص مجلدات الجلسات الفرعية الثلاثة:")
                        for sess in sessions_data:
                            att_files = sess.get("attendance", {}).get("files", [])
                            doc_files = sess.get("documentation", {}).get("files", [])
                            rep_files = sess.get("report", {}).get("files", [])
                            
                            is_complete = len(att_files) > 0 and len(doc_files) > 0 and len(rep_files) > 0
                            status_tag = "✅ مكتملة" if is_complete else "⚠️ ناقصة"

                            sess_title = sess.get("session_name", "جلسة بدون عنوان")
                            
                            with st.expander(f"📌 المسار الكامل: \u200e{sess_title}\u200f — الحالة: {status_tag}"):
                                col_f1, col_f2, col_f3 = st.columns(3)
                                with col_f1:
                                    st.write("**📄 ورقة الحضور:**")
                                    if att_files:
                                        for f in att_files: st.caption(f"• {f['name']}")
                                    else:
                                        st.error("❌ ملف الحضور مفقود")
                                with col_f2:
                                    st.write("**🖼️ صور التوثيق (Documentation):**")
                                    if doc_files:
                                        for f in doc_files: st.caption(f"• {f['name']}")
                                    else:
                                        st.warning("⚠️ صور التوثيق غير موجودة")
                                with col_f3:
                                    st.write("**📑 التقرير:**")
                                    if rep_files:
                                        for f in rep_files: st.caption(f"• {f['name']}")
                                    else:
                                        st.error("❌ التقرير مفقود")

                    elif current_view == "matching":
                        st.markdown("#### ⚖️ مطابقة المحتوى الفعلي لأوراق الحضور مع التقارير:")
                        st.info("💡 اضغط على زر **(🔄 إعادة مطابقة)** بجانب الجلسة التي تريد تحليلها.")
                        
                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح Gemini API في الشريط الجانبي أولاً.")
                        else:
                            all_logs = load_scan_logs()
                            if p_name not in all_logs:
                                all_logs[p_name] = {}

                            for s_idx, sess in enumerate(sessions_data):
                                sess_title = sess.get("session_name", "جلسة")
                                has_cached = "cached_metrics" in sess
                                
                                col_info, col_btn = st.columns([4, 1])
                                with col_info:
                                    st.markdown(f"**📌 الجلسة: \u200e{sess_title}\u200f**")
                                with col_btn:
                                    reparse_clicked = st.button("🔄 إعادة مطابقة", key=f"reparse_{p_name}_{s_idx}")

                                if reparse_clicked or not has_cached:
                                    with st.spinner(f"جاري مطابقة جلسة: {sess_title}..."):
                                        att_vals, rep_vals, diff_vals = analyze_session_files_with_ai(service, sess, GEMINI_API_KEY, force_refresh=reparse_clicked)
                                        
                                        all_logs[p_name][sess_title] = {
                                            "attendance": att_vals,
                                            "report": rep_vals,
                                            "differences": diff_vals,
                                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                                        }
                                        save_scan_logs(all_logs)
                                else:
                                    att_vals, rep_vals, diff_vals = sess["cached_metrics"]

                                comparison_data = {
                                    "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال (Men)", "النساء (Women)", "الأولاد (Boys)", "الفتيات (Girls)", "ذوي الاحتياجات (PWD)"],
                                    "ورقة الحضور (محتوى الملف)": att_vals,
                                    "التقرير (محتوى الملف)": rep_vals,
                                    "حالة التدقيق / الفروقات": diff_vals
                                }
                                with st.expander(f"🔍 عرض تفاصيل الجدول: \u200e{sess_title}\u200f"):
                                    st.table(comparison_data)
                                
                                st.markdown("---")

                    elif current_view == "logs":
                        st.markdown("#### 📂 سجل الفحص والمطابقة المحفوظ حسب المشروع والنشاط:")
                        all_logs = load_scan_logs()
                        if not all_logs or p_name not in all_logs or not all_logs[p_name]:
                            st.info("💡 لا توجد سجلات فحص محفوظة لهذا المشروع حتى الآن.")
                        else:
                            project_logs = all_logs[p_name]
                            for sess_name, log_data in project_logs.items():
                                expander_title = f"📌 المسار الكامل: \u200e{sess_name}\u200f 🕒 (آخر فحص: \u200e{log_data.get('timestamp', 'غير معروف')}\u200f)"
                                with st.expander(expander_title):
                                    saved_comp_data = {
                                        "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال (Men)", "النساء (Women)", "الأولاد (Boys)", "الفتيات (Girls)", "ذوي الاحتياجات (PWD)"],
                                        "ورقة الحضور": log_data.get("attendance", []),
                                        "التقرير": log_data.get("report", []),
                                        "حالة التدقيق / الفروقات": log_data.get("differences", [])
                                    }
                                    st.table(saved_comp_data)
                                    if st.button(f"🗑️ حذف هذا السجل", key=f"del_log_{p_name}_{sess_name}"):
                                        del all_logs[p_name][sess_name]
                                        save_scan_logs(all_logs)
                                        st.success("تم حذف السجل بنجاح!")
                                        st.rerun()

                    elif current_view == "gap_analysis":
                        st.markdown("#### 📊 تقرير تحليل الفجوات والمخاطر التلقائي للمشروع:")
                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح Gemini API في الشريط الجانبي أولاً.")
                        else:
                            if st.button("🚀 توليد تقرير الفجوات الشامل بالذكاء الاصطناعي", key=f"gen_gap_{p_name}"):
                                with st.spinner("جاري تحليل كافة جلسات ومرفقات المشروع وإعداد تقرير الفجوات التلقائي..."):
                                    gap_context = f"مشروع: {p_name} | المجلد الرئيسي: {current_folder['name']}\n\n"
                                    for s in sessions_data:
                                        s_title = s.get('session_name', 'جلسة')
                                        att_files = s.get("attendance", {}).get("files", [])
                                        doc_files = s.get("documentation", {}).get("files", [])
                                        rep_files = s.get("report", {}).get("files", [])
                                        
                                        att_v, rep_v, diff_v = analyze_session_files_with_ai(service, s, GEMINI_API_KEY, force_refresh=False)
                                        
                                        gap_context += f"### المسار الكامل: {s_title}\n"
                                        gap_context += f"- مرفقات الحضور: {'موجودة (' + str(len(att_files)) + ')' if att_files else 'مفقودة ❌'}\n"
                                        gap_context += f"- صور التوثيق: {'موجودة (' + str(len(doc_files)) + ')' if doc_files else 'مفقودة ⚠️'}\n"
                                        gap_context += f"- التقرير: {'موجود (' + str(len(rep_files)) + ')' if rep_files else 'مفقودة ❌'}\n"
                                        gap_context += f"- بيانات الحضور المستخلصة: {att_v}\n"
                                        gap_context += f"- بيانات التقرير المستخلصة: {rep_v}\n"
                                        gap_context += f"- الفروقات وحالة التدقيق: {diff_v}\n\n"

                                    gap_prompt = (
                                        "أنت خبير محترف في المتابعة والتقييم (M&E) لمشاريع الإغاثة والتنمية.\n"
                                        "بناءً على بيانات الجلسات والمرفقات والفروقات المستخرجة للمشروع أدناه، قم إعداد "
                                        "تقرير تحليل فجوات ومخاطر شامل ومهني باللغة العربية.\n\n"
                                        f"بيانات المشروع:\n{gap_context}"
                                    )

                                    try:
                                        client_g = genai.Client(api_key=GEMINI_API_KEY.strip())
                                        gap_res = call_gemini_with_retry(
                                            client=client_g,
                                            model="gemini-2.5-flash",
                                            contents=gap_prompt
                                        )
                                        st.session_state[f"gap_report_{p_name}"] = gap_res.text
                                    except Exception as e:
                                        st.error(f"❌ حدث خطأ أثناء توليد تقرير الفجوات: {e}")

                            if f"gap_report_{p_name}" in st.session_state:
                                st.markdown("---")
                                st.markdown(st.session_state[f"gap_report_{p_name}"])

                    elif current_view == "stats":
                        st.markdown("#### 📊 إحصائية المستفيدين لكل جلسة على حدة بناءً على محتوى الملفات الحقيقي:")
                        
                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح Gemini API في الشريط الجانبي أولاً.")
                        else:
                            tot_men_all, tot_women_all, tot_boys_all, tot_girls_all, tot_pwd_all = 0, 0, 0, 0, 0
                            
                            for s in sessions_data:
                                sess_title = s.get("session_name", "جلسة بدون عنوان")
                                att_v, _, _ = analyze_session_files_with_ai(service, s, GEMINI_API_KEY, force_refresh=False)
                                
                                s_men = extract_number(att_v[2])
                                s_women = extract_number(att_v[3])
                                s_boys = extract_number(att_v[4])
                                s_girls = extract_number(att_v[5])
                                s_pwd = extract_number(att_v[6])
                                
                                tot_men_all += s_men
                                tot_women_all += s_women
                                tot_boys_all += s_boys
                                tot_girls_all += s_girls
                                tot_pwd_all += s_pwd
                                
                                with st.expander(f"📌 المسار الكامل: \u200e{sess_title}\u200f", expanded=True):
                                    c1, c2, c3, c4, c5 = st.columns(5)
                                    c1.metric("👨 رجال", str(s_men))
                                    c2.metric("👩 نساء", str(s_women))
                                    c3.metric("👧 فتيات", str(s_girls))
                                    c4.metric("👶 أطفال ذكور", str(s_boys))
                                    c5.metric("♿ ذوي الاحتياجات", str(s_pwd))
                            
                            st.markdown("---")
                            st.markdown("### 📈 إجمالي المستفيدين لجميع الجلسات المعروضة:")
                            tot_c1, tot_c2, tot_c3, tot_c4, tot_c5 = st.columns(5)
                            tot_c1.metric("👨 إجمالي الرجال", str(tot_men_all))
                            tot_c2.metric("👩 إجمالي النساء", str(tot_women_all))
                            tot_c3.metric("👧 إجمالي الفتيات", str(tot_girls_all))
                            tot_c4.metric("👶 إجمالي الأطفال", str(tot_boys_all))
                            tot_c5.metric("♿ إجمالي ذوي الاحتياجات", str(tot_pwd_all))

                    elif current_view == "chat":
                        st.markdown("---")
                        st.subheader("💬 دردشة المساعد الذكي لمراجعة محتوى الجلسات")

                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح الذكاء الاصطناعي في الشريط الجانبي لتفعيل المحادثة.")
                        else:
                            chat_history_key = f"messages_{p_name}"
                            if chat_history_key not in st.session_state:
                                st.session_state[chat_history_key] = []

                            for message in st.session_state[chat_history_key]:
                                with st.chat_message(message["role"]):
                                    st.write(message["content"])

                            if prompt_text := st.chat_input("وجه سؤالك للذكاء الاصطناعي حول محتوى ملفات الجلسات..."):
                                st.session_state[chat_history_key].append({"role": "user", "content": prompt_text})
                                with st.chat_message("user"):
                                    st.write(prompt_text)

                                context_text = f"مشروع: {p_name} | المجلد: {current_folder['name']}\n"
                                context_text += f"عدد الجلسات الإجمالي: {len(sessions_data)}\n\n"
                                
                                for s in sessions_data:
                                    s_title = s.get('session_name', '')
                                    att_v, rep_v, _ = analyze_session_files_with_ai(service, s, GEMINI_API_KEY, force_refresh=False)
                                    context_text += f"- {s_title}:\n"
                                    context_text += f"  * الحضور المستخلص: {att_v}\n"
                                    context_text += f"  * التقرير المستخلص: {rep_v}\n"

                                with st.chat_message("assistant"):
                                    with st.spinner("جاري تحليل ومراجعة محتوى الملفات والإجابة..."):
                                        ai_prompt = f"أنت مساعد ذكي مدقق لمشاريع المتابعة والتقييم.\nأجب باللغة العربية بناءً على محتوى الملفات الحقيقي المستخلص:\n{context_text}\nسؤال المستخدم: {prompt_text}"
                                        
                                        try:
                                            client = genai.Client(api_key=GEMINI_API_KEY.strip())
                                            response = call_gemini_with_retry(
                                                client=client,
                                                model="gemini-2.5-flash",
                                                contents=ai_prompt
                                            )
                                            ai_response = response.text
                                            st.write(ai_response)
                                            st.session_state[chat_history_key].append({"role": "assistant", "content": ai_response})
                                        except Exception as e:
                                            st.error(f"❌ حدث خطأ في المحادثة: {e}")

st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>", 
    unsafe_allow_html=True
)
