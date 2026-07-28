import streamlit as st
import json
import os
import time
import io
import tempfile
import re
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai
from google.genai.errors import ClientError

logging.basicConfig(level=logging.INFO)

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
        except Exception as e:
            logging.exception("Failed to load saved projects")
            return {}
    return {}


def _atomic_write(path, data):
    # Write JSON atomically to reduce risk of corruption
    dirn = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dirn)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmpf:
            json.dump(data, tmpf, ensure_ascii=False, indent=4)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def save_projects(projects_dict):
    try:
        _atomic_write(CONFIG_FILE, projects_dict)
    except Exception as e:
        logging.exception("خطأ أثناء حفظ البيانات في %s", CONFIG_FILE)
        st.error(f"خطأ أثناء حفظ البيانات: {e}")


def load_scan_logs():
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.exception("Failed to load scan logs")
            return {}
    return {}


def save_scan_logs(logs_dict):
    try:
        _atomic_write(LOGS_FILE, logs_dict)
    except Exception as e:
        logging.exception("Failed to save scan logs")


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
        logging.exception("Error creating Drive service")
        st.error(f"❌ خطأ في الاتصال: {e}")
        return None


def get_folder_contents(service, folder_id):
    """Return (folders, files) for a given folder id. Handles pagination."""
    if service is None:
        logging.warning("get_folder_contents called with service=None")
        return [], []

    query = f"'{folder_id}' in parents and trashed = false"
    folders = []
    files = []
    page_token = None

    try:
        while True:
            results = service.files().list(
                q=query,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                spaces='drive',
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageToken=page_token
            ).execute()

            for item in results.get("files", []):
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    folders.append(item)
                else:
                    files.append(item)

            page_token = results.get('nextPageToken')
            if not page_token:
                break

    except Exception as e:
        logging.exception("خطأ أثناء قراءة المجلد %s", folder_id)
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
    if service is None:
        logging.warning("get_file_content_bytes called with service=None")
        return None

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
        logging.exception("Failed to download file %s", file_id)
        return None


def extract_number(text):
    if not text:
        return 0
    match = re.search(r'\d+', str(text))
    return int(match.group()) if match else 0


def analyze_session_files_with_ai(service, session_info, api_key):
    """Analyze attendance and report files for a session using GenAI.
    Returns a tuple (attendance_data, report_data, differences).
    """
    if "cached_metrics" in session_info:
        return session_info["cached_metrics"]

    att_files = session_info.get("attendance", {}).get("files", [])
    rep_files = session_info.get("report", {}).get("files", [])

    error_result = (
        ["❌ غير متوفر"] * 7,
        ["❌ غير متوفر"] * 7,
        ["❌ ملفات الحضور أو التقرير مفقودة"] + ["❌ خطأ"] * 6
    )

    if not api_key or (not att_files and not rep_files):
        session_info["cached_metrics"] = error_result
        return error_result

    client = genai.Client(api_key=api_key.strip())
    uploaded_refs = []

    try:
        # Upload files (but limit to avoid huge costs)
        MAX_FILES = 6
        files_to_upload = (att_files + rep_files)[:MAX_FILES]

        for f in files_to_upload:
            b = get_file_content_bytes(service, f["id"], f.get("mimeType", ""))
            if not b:
                continue

            # choose suffix conservatively
            if "pdf" in f.get("mimeType", ""):
                suffix = ".pdf"
            elif "image" in f.get("mimeType", ""):
                suffix = ".png"
            elif "spreadsheet" in f.get("mimeType", "") or f.get("name", "").lower().endswith('.csv'):
                suffix = ".csv"
            elif "document" in f.get("mimeType", "") or f.get("name", "").lower().endswith('.docx'):
                suffix = ".txt"
            else:
                suffix = ".bin"

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(b)
                tmp_path = tmp.name

            try:
                up_file = client.files.upload(file=tmp_path)
                # collect a stable reference for prompt and cleanup
                ref = None
                try:
                    # try attributes first
                    ref = getattr(up_file, 'name', None) or getattr(up_file, 'id', None)
                except Exception:
                    pass
                if not ref:
                    try:
                        ref = up_file.get('name') if isinstance(up_file, dict) else str(up_file)
                    except Exception:
                        ref = str(up_file)

                uploaded_refs.append({"ref": ref, "remote_obj": up_file})
            except Exception:
                logging.exception("Failed uploading file to GenAI")
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        if not uploaded_refs:
            session_info["cached_metrics"] = error_result
            return error_result

        # Build a clear prompt (no truncated parts)
        prompt_parts = [
            "أنت مدقق ومراجع دقيق لمستندات المشاريع والمخيمات.",
            "اقرأ محتوى الملفات المرفوعة واستخرج القيم التالية بدقة: التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات.",
            "تحذير: لا تفترض أو تخمن أي أرقام إذا كانت البيانات غير واضحة — أعلِم بأن الحقل مفقود بدلاً من التخمين.",
            "قم بمقارنة بيانات ورقة الحضور مع بيانات التقرير لكل بند واكتب نتيجة المقارنة (مثلاً: مطابق، يوجد فرق N، ناقص/زائد).",
            "أجب بصيغة JSON فقط بالشكل التالي: {\n  \"attendance_data\": [...],\n  \"report_data\": [...],\n  \"differences\": [...]\n} ولا تضف نصًا آخر."
        ]

        prompt_text = "\n\n".join(prompt_parts)
        prompt_text += "\n\nالملفات المرفوعة (مراجع):\n"
        for r in uploaded_refs:
            prompt_text += f"- {r['ref']}\n"

        # Send to model
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt_text,
        )

        text_res = getattr(response, 'text', None) or str(response)
        cleaned = text_res.replace("```json", "").replace("```", "").strip()

        try:
            data = json.loads(cleaned)
        except Exception:
            logging.exception("Failed to parse JSON from GenAI response")
            session_info["cached_metrics"] = error_result
            return error_result

        result = (
            data.get("attendance_data", error_result[0]),
            data.get("report_data", error_result[1]),
            data.get("differences", error_result[2])
        )
        session_info["cached_metrics"] = result
        return result

    except ClientError as ce:
        logging.exception("GenAI ClientError")
        error_msg = f"ClientError ({getattr(ce, 'code', 'N/A')}): {str(ce)}"
        err_tuple = (
            ["❌ خطأ API"] * 7,
            ["❌ خطأ API"] * 7,
            [f"❌ {error_msg[:100]}"] + ["❌ خطأ"] * 6
        )
        session_info["cached_metrics"] = err_tuple
        return err_tuple
    except Exception as e:
        logging.exception("Unexpected error in analyze_session_files_with_ai")
        error_msg = str(e)
        err_tuple = (
            ["❌ تعذر القراءة"] * 7,
            ["❌ تعذر القراءة"] * 7,
            [f"❌ سبب الخطأ: {error_msg[:120]}"] + ["❌ خطأ"] * 6
        )
        session_info["cached_metrics"] = err_tuple
        return err_tuple
    finally:
        # Attempt cleanup of uploaded files if the client returned removable objects
        for r in uploaded_refs:
            remote = r.get('remote_obj')
            if not remote:
                continue
            try:
                # try several possible delete methods
                if hasattr(remote, 'name'):
                    client.files.delete(name=getattr(remote, 'name'))
                elif isinstance(remote, dict) and remote.get('id'):
                    client.files.delete(id=remote.get('id'))
                else:
                    # best-effort fallback
                    try:
                        client.files.delete(name=str(remote))
                    except Exception:
                        pass
            except Exception:
                logging.exception("Failed to delete uploaded remote file")


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

if service is None:
    st.error("خطأ: خدمة Google Drive غير متاحة - يرجى التحقق من إعدادات Secrets.")

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

            sub_folders, direct_files = get_folder_contents(service, current_folder["id"]) if service else ([], [])

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
                f"⚡ جلب وتحليل محتوى ملفات الجلسات تحت ({current_folder['name']})",
                key=f"fetch_btn_{p_name}",
                type="primary"
            )

            if btn_fetch:
                with st.spinner("جاري تحديد مجلدات الجلسات واستخراج وقراءة محتوى الملفات الفعلية..."):
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
                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح Gemini API في الشريط الجانبي أولاً.")
                        else:
                            all_logs = load_scan_logs()
                            if p_name not in all_logs:
                                all_logs[p_name] = {}

                            for sess in sessions_data:
                                sess_title = sess.get("session_name", "جلسة")
                                att_vals, rep_vals, diff_vals = analyze_session_files_with_ai(service, sess, GEMINI_API_KEY)

                                all_logs[p_name][sess_title] = {
                                    "attendance": att_vals,
                                    "report": rep_vals,
                                    "differences": diff_vals,
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                                }

                                comparison_data = {
                                    "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال (Men)", "النساء (Women)", "الأولاد (Boys)", "الفتيات (Girls)", "ذوي احتياجات"],
                                    "ورقة الحضور (محتوى الملف)": att_vals,
                                    "التقرير (محتوى الملف)": rep_vals,
                                    "حالة التدقيق / الفروقات": diff_vals
                                }
                                with st.expander(f"🔍 المسار الكامل: \u200e{sess_title}\u200f"):
                                    st.table(comparison_data)

                            save_scan_logs(all_logs)
                            st.success("💾 تم حفظ نتائج المطابقة والفحص في 'سجل الفحص' بنجاح!")

                    elif current_view == "logs":
                        st.markdown("#### 📂 سجل الفحص والمطابقة المحفوظ حسب المشروع والنشاط:")
                        all_logs = load_scan_logs()
                        if not all_logs or p_name not in all_logs or not all_logs[p_name]:
                            st.info("💡 لا توجد سجلات فحص محفوظة لهذا المشروع حتى الآن. قم بالانتقال إلى (2️⃣ مطابقة الحضور) لبدء الفحص.")
                        else:
                            project_logs = all_logs[p_name]
                            for sess_name, log_data in project_logs.items():
                                expander_title = f"📌 المسار الكامل: \u200e{sess_name}\u200f 🕒 (آخر فحص: \u200e{log_data.get('timestamp', 'غير معروف')}\u200f)"
                                with st.expander(expander_title):
                                    saved_comp_data = {
                                        "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال (Men)", "النساء (Women)", "الأولاد (Boys)", "الفتيات (Girls)", "ذوي احتياجات"],
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
                                        att_v, rep_v, diff_v = analyze_session_files_with_ai(service, s, GEMINI_API_KEY)

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
                                        "تقرير تحليل فجوات ومخاطر شامل ومهني باللغة العربية.\n"
                                        "يجب أن يتضمن التقرير:\n"
                                        "1. ملخص تنفيذي لحالة التوثيق والاكتمال في المشروع.\n"
                                        "2. أبرز الفجوات والمشاكل العددية أو النقص في الملفات لكل جلسة.\n"
                                        "3. تقييم المخاطر الميدانية والتوثيقية.\n"
                                        "4. توصيات عملية وعاجلة لفريق المشروع لمعالجة هذه الفجوات.\n\n"
                                        f"بيانات المشروع:\n{gap_context}"
                                    )

                                    try:
                                        client_g = genai.Client(api_key=GEMINI_API_KEY.strip())
                                        gap_res = client_g.models.generate_content(
                                            model="gemini-3.5-flash",
                                            contents=gap_prompt
                                        )
                                        st.session_state[f"gap_report_{p_name}"] = getattr(gap_res, 'text', str(gap_res))
                                    except Exception as e:
                                        logging.exception("Failed generating gap report")
                                        st.error(f"❌ حدث خطأ أثناء توليد التقرير: {e}")

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
                                att_v, _, _ = analyze_session_files_with_ai(service, s, GEMINI_API_KEY)

                                s_men = extract_number(att_v[2]) if len(att_v) > 2 else 0
                                s_women = extract_number(att_v[3]) if len(att_v) > 3 else 0
                                s_boys = extract_number(att_v[4]) if len(att_v) > 4 else 0
                                s_girls = extract_number(att_v[5]) if len(att_v) > 5 else 0
                                s_pwd = extract_number(att_v[6]) if len(att_v) > 6 else 0

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
                                    att_v, rep_v, _ = analyze_session_files_with_ai(service, s, GEMINI_API_KEY)
                                    context_text += f"- {s_title}:\n"
                                    context_text += f"  * الحضور المستخلص: {att_v}\n"
                                    context_text += f"  * التقرير المستخلص: {rep_v}\n"

                                with st.chat_message("assistant"):
                                    with st.spinner("جاري تحليل ومراجعة محتوى الملفات والإجابة..."):
                                        ai_prompt = (
                                            "أنت مساعد ذكي مدقق لمشاريع المتابعة والتقييم.\n"
                                            "استخدم المعلومات والمخرجات المستخلصة من الملفات لتقديم إجابة مفصلة ومفيدة باللغة العربية.\n"
                                            "أدرج تحليلاً موجزاً، نقاط الفجوات المحتملة، وتوصيات عملية قصيرة.\n"
                                            f"سياق المشروع:\n{context_text}\nسؤال المستخدم:\n{prompt_text}"
                                        )

                                        try:
                                            client = genai.Client(api_key=GEMINI_API_KEY.strip())
                                            interaction = client.interactions.create(
                                                model="gemini-3.5-flash",
                                                input=ai_prompt,
                                            )
                                            ai_response = getattr(interaction, 'output_text', None) or str(interaction)
                                            st.write(ai_response)
                                            st.session_state[chat_history_key].append({"role": "assistant", "content": ai_response})
                                        except ClientError as ce:
                                            logging.exception("GenAI usage error in chat")
                                            st.error(f"❌ خطأ حد الاستخدام في الخطة المجانية (كود {getattr(ce, 'code', 'N/A')}): يرجى الانتظار ثم إعادة المحاولة.")
                                        except Exception as e:
                                            logging.exception("Unexpected chat error")
                                            st.error(f"❌ حدث خطأ غير متوقع: {e}")

st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>",
    unsafe_allow_html=True
)
