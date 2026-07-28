import streamlit as st
import json
import os
import time
import io
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import google.generativeai as genai

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
    except Exception:
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

ATTENDANCE_KEYWORDS = ["attendance", "attend", "حضور", "غياب", "كشف", "اسماء", "أسماء", "مشاركين", "مستفيدين", "sheet"]
REPORT_KEYWORDS = ["report", "reports", "تقرير", "تقارير", "ملخص", "نشاط", "إنجاز", "انجاز", "إحصائية"]
DOC_KEYWORDS = ["doc", "photo", "photos", "image", "images", "صور", "توثيق", "أرشيف", "لقطات"]

def is_sub_component(name):
    name_lower = name.lower().strip()
    all_kw = ATTENDANCE_KEYWORDS + REPORT_KEYWORDS + DOC_KEYWORDS
    return any(kw in name_lower for kw in all_kw)

def categorize_files_smart(service, session_folder):
    sub_folders, direct_files = get_folder_contents(service, session_folder["id"])
    
    session_data = {
        "session_id": session_folder["id"],
        "session_name": session_folder["name"],
        "attendance": {"folder": None, "files": []},
        "documentation": {"folder": None, "files": []},
        "report": {"folder": None, "files": []},
        "extra_files": list(direct_files)
    }
    
    for sf in sub_folders:
        sf_name = sf["name"].lower().strip()
        _, files = get_folder_contents(service, sf["id"])
        
        if any(k in sf_name for k in ATTENDANCE_KEYWORDS):
            session_data["attendance"]["folder"] = sf
            session_data["attendance"]["files"].extend(files)
        elif any(k in sf_name for k in REPORT_KEYWORDS):
            session_data["report"]["folder"] = sf
            session_data["report"]["files"].extend(files)
        elif any(k in sf_name for k in DOC_KEYWORDS):
            session_data["documentation"]["folder"] = sf
            session_data["documentation"]["files"].extend(files)
        else:
            session_data["extra_files"].extend(files)

    return session_data

def fetch_structured_sessions(service, target_folder_id, base_path_str=""):
    sessions_list = []
    top_folders, direct_files = get_folder_contents(service, target_folder_id)
    
    for folder in top_folders:
        if is_sub_component(folder["name"]):
            continue
            
        child_folders, child_files = get_folder_contents(service, folder["id"])
        has_sub = any(is_sub_component(cf["name"]) for cf in child_folders) or len(child_files) > 0
        
        if has_sub:
            parsed = categorize_files_smart(service, folder)
            parsed["session_name"] = f"{base_path_str} ➔ {folder['name']}" if base_path_str else folder['name']
            sessions_list.append(parsed)
        else:
            for cf in child_folders:
                if is_sub_component(cf["name"]):
                    continue
                parsed = categorize_files_smart(service, cf)
                parsed["session_name"] = f"{base_path_str} ➔ {folder['name']} ➔ {cf['name']}" if base_path_str else f"{folder['name']} ➔ {cf['name']}"
                sessions_list.append(parsed)
                    
    return sessions_list

def get_file_bytes_and_mime(service, file_id, original_mime):
    try:
        if original_mime == 'application/vnd.google-apps.document':
            request = service.files().export_media(fileId=file_id, mimeType='text/plain')
            target_mime = 'text/plain'
        elif original_mime == 'application/vnd.google-apps.spreadsheet':
            request = service.files().export_media(fileId=file_id, mimeType='text/csv')
            target_mime = 'text/csv'
        else:
            request = service.files().get_media(fileId=file_id)
            target_mime = original_mime
        
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        
        if "pdf" in target_mime:
            target_mime = "application/pdf"
        elif "png" in target_mime:
            target_mime = "image/png"
        elif "jpg" in target_mime or "jpeg" in target_mime:
            target_mime = "image/jpeg"
        elif "text" in target_mime or "csv" in target_mime:
            target_mime = "text/plain"
            
        return fh.read(), target_mime
    except Exception:
        return None, None

def extract_number(text):
    if not text:
        return 0
    match = re.search(r'\d+', str(text))
    return int(match.group()) if match else 0

def analyze_session_inline(service, session_info, api_key, model_choice="gemini-1.5-flash"):
    """تحليل ذكي ومستقر يضمن عدم حدوث خطأ 404"""
    all_files_dict = {}
    
    for category in ["attendance", "report", "documentation"]:
        for f in session_info.get(category, {}).get("files", []):
            all_files_dict[f["id"]] = f
            
    for f in session_info.get("extra_files", []):
        all_files_dict[f["id"]] = f

    all_files = list(all_files_dict.values())

    error_result = (
        ["❌ غير متوفر"] * 7,
        ["❌ غير متوفر"] * 7,
        ["❌ لا توجد ملفات داخل مجلد الجلسة"] + ["❌ خطأ"] * 6
    )

    if not api_key or not all_files:
        return error_result

    genai.configure(api_key=api_key.strip())
    
    prompt_instruction = (
        "أنت مدقق ومراجع دقيق لمستندات المشاريع والمخيمات.\n"
        "المرفقات التالية تحتوي على ملفات الجلسة (كشوف الحضور، تقارير الإنجاز، أوراق التوثيق).\n"
        "قم بقراءة محتوى جميع الملفات المرفقة واستخرج البيانات التالية بدقة تامة:\n"
        "1. بيانات كشف الحضور (التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات).\n"
        "2. بيانات التقرير (التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات).\n"
        "3. قارن بين أرقام الحضور وأرقام التقرير وحدد الفروقات أو حالة التطابق لكل بند.\n\n"
        "تنبيه صارم: إذا كان أحد الملفين مفقوداً أو لم تجد به أرقاماً، ضع '❌ غير متوفر' بدلاً من وضع أصفار أو التخمين.\n"
        "أجب بصيغة JSON صارمة فقط بالشكل التالي دون أي نصوص خارج JSON:\n"
        "{\n"
        '  "attendance_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
        '  "report_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
        '  "differences": ["مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق..."]\n'
        "}"
    )

    contents = [prompt_instruction]

    for f in all_files:
        b_data, m_type = get_file_bytes_and_mime(service, f["id"], f["mimeType"])
        if b_data and m_type:
            try:
                contents.append({"mime_type": m_type, "data": b_data})
            except Exception:
                pass

    if len(contents) == 1:
        return error_result

    # نماذج رسمية ثابتة ومضمونة عدم إرجاع 404
    candidate_models = [model_choice, "gemini-1.5-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro"]
    candidate_models = list(dict.fromkeys(candidate_models))

    last_err = ""
    for m_name in candidate_models:
        try:
            model = genai.GenerativeModel(m_name)
            response = model.generate_content(contents)
            text_res = response.text
            cleaned = text_res.replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            
            return (
                data.get("attendance_data", error_result[0]),
                data.get("report_data", error_result[1]),
                data.get("differences", error_result[2])
            )
        except Exception as e:
            last_err = str(e)
            continue

    return (
        ["❌ خطأ API"] * 7,
        ["❌ خطأ API"] * 7,
        [f"❌ {last_err[:80]}"] + ["❌ خطأ"] * 6
    )

# --- الواجهة الرئيسية ---
st.title("📊 نظام إدارة ومتابعة المشاريع الذكي (ME Assistant)")
st.markdown("---")

if "projects" not in st.session_state:
    st.session_state.projects = load_saved_projects()

with st.sidebar:
    st.header("⚙️ إعدادات النظام والمشاريع")
    default_api_key = st.secrets.get("GEMINI_API_KEY", "")
    GEMINI_API_KEY = st.text_input("مفتاح Gemini API:", value=default_api_key, type="password")
    
    selected_model = st.selectbox(
        "اختر إصدار النموذج:",
        ["gemini-1.5-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro"],
        index=0
    )
    
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
            
            if len(current_trail) > 1:
                if st.button("⬅️ العودة للمجلد السابق", key=f"back_{p_name}"):
                    st.session_state[path_key].pop()
                    st.rerun()
                        
            sub_folders, _ = get_folder_contents(service, current_folder["id"])
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
            btn_fetch = st.button(f"⚡ جلب وتحليل الجلسات تحت ({current_folder['name']})", key=f"fetch_btn_{p_name}", type="primary")

            if btn_fetch:
                with st.spinner("جاري جلب وتحديد مجلدات الجلسات..."):
                    base_path_str = " ➔ ".join([node["name"] for node in current_trail])
                    sessions = fetch_structured_sessions(service, current_folder["id"], base_path_str)
                    st.session_state[f"data_{p_name}"] = sessions

            sessions_data = st.session_state.get(f"data_{p_name}", None)
            
            if sessions_data is not None:
                if not sessions_data:
                    st.warning("لم يتم العثور على جلسات داخل هذا المجلد.")
                else:
                    st.success(f"تم اكتشاف {len(sessions_data)} جلسة/جلسات!")
                    st.markdown("---")
                    
                    st.subheader("🛠️ أدوات التحليل والمطابقة الذكية")
                    b1, b2, b3, b4, b5, b6, b7 = st.columns(7)
                    
                    if b1.button("1️⃣ فحص المرفقات", key=f"b1_{p_name}"): st.session_state[f"view_{p_name}"] = "attachments"
                    if b2.button("2️⃣ مطابقة الحضور", key=f"b2_{p_name}"): st.session_state[f"view_{p_name}"] = "matching"
                    if b3.button("3️⃣ إحصائية الفئات", key=f"b3_{p_name}"): st.session_state[f"view_{p_name}"] = "stats"
                    if b4.button("4️⃣ سجل الفحص 📋", key=f"b4_{p_name}"): st.session_state[f"view_{p_name}"] = "logs"
                    if b5.button("5️⃣ تقرير الفجوات 📊", key=f"b5_{p_name}"): st.session_state[f"view_{p_name}"] = "gap_analysis"
                    if b6.button("6️⃣ المساعد الذكي 💬", key=f"b6_{p_name}"): st.session_state[f"view_{p_name}"] = "chat"
                    if b7.button("🌐 تحليل خارجي", key=f"b7_{p_name}"): st.session_state[f"view_{p_name}"] = "external"

                    current_view = st.session_state.get(f"view_{p_name}", "attachments")

                    if current_view == "attachments":
                        st.markdown("#### 📋 تشخيص وقائمة ملفات الجلسات:")
                        for sess in sessions_data:
                            att_files = sess.get("attendance", {}).get("files", [])
                            rep_files = sess.get("report", {}).get("files", [])
                            doc_files = sess.get("documentation", {}).get("files", [])
                            extra_files = sess.get("extra_files", [])
                            
                            sess_title = sess.get("session_name", "جلسة")
                            
                            with st.expander(f"📌 المسار: \u200e{sess_title}\u200f"):
                                c1, c2, c3, c4 = st.columns(4)
                                with c1:
                                    st.write("**📄 أوراق الحضور:**")
                                    for f in att_files: st.caption(f"• {f['name']}")
                                    if not att_files: st.caption("لا يوجد")
                                with c2:
                                    st.write("**📑 التقارير:**")
                                    for f in rep_files: st.caption(f"• {f['name']}")
                                    if not rep_files: st.caption("لا يوجد")
                                with c3:
                                    st.write("**🖼️ التوثيق:**")
                                    for f in doc_files: st.caption(f"• {f['name']}")
                                    if not doc_files: st.caption("لا يوجد")
                                with c4:
                                    st.write("**📂 ملفات إضافية:**")
                                    for f in extra_files: st.caption(f"• {f['name']}")
                                    if not extra_files: st.caption("لا يوجد")

                    elif current_view == "matching":
                        st.markdown(f"#### ⚖️ مطابقة المحتوى الفعلي تلقائياً عبر API:")
                        all_logs = load_scan_logs()
                        if p_name not in all_logs: all_logs[p_name] = {}

                        col_m1, col_m2 = st.columns([2, 1])
                        run_analysis = col_m1.button("🚀 بدء / استكمال التحليل الذكي للجلسات", key=f"run_m_{p_name}", type="primary")
                        if col_m2.button("🔄 إعادة تحليل كافة الجلسات من الجديد", key=f"force_m_{p_name}"):
                            all_logs[p_name] = {}
                            save_scan_logs(all_logs)
                            st.rerun()

                        if run_analysis:
                            progress_bar = st.progress(0)
                            for i, sess in enumerate(sessions_data):
                                sess_title = sess.get("session_name", "جلسة")
                                
                                st.write(f"جاري قراءة وتحليل ملفات: **{sess_title}**...")
                                att_vals, rep_vals, diff_vals = analyze_session_inline(
                                    service, sess, GEMINI_API_KEY, model_choice=selected_model
                                )
                                
                                all_logs[p_name][sess_title] = {
                                    "attendance": att_vals,
                                    "report": rep_vals,
                                    "differences": diff_vals,
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                                }
                                save_scan_logs(all_logs)
                                progress_bar.progress((i + 1) / len(sessions_data))
                                time.sleep(1)

                            st.success("✅ اكتمل تحليل وتحقيق كافة الجلسات وحفظ نتائجها!")

                        for sess in sessions_data:
                            sess_title = sess.get("session_name", "جلسة")
                            log_entry = all_logs[p_name].get(sess_title, None)
                            
                            with st.expander(f"🔍 المسار الكامل: \u200e{sess_title}\u200f"):
                                if log_entry:
                                    st.table({
                                        "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال (Men)", "النساء (Women)", "الأولاد (Boys)", "الفتيات (Girls)", "ذوي الاحتياجات (PWD)"],
                                        "ورقة الحضور": log_entry.get("attendance", []),
                                        "التقرير": log_entry.get("report", []),
                                        "حالة التدقيق / الفروقات": log_entry.get("differences", [])
                                    })
                                else:
                                    st.info("اضغط على 'بدء / استكمال التحليل الذكي' لبدء القراءة والمطابقة.")

                    elif current_view == "external":
                        st.markdown("#### 🌐 التحليل الخارجي عن طريق Gemini Web المباشر:")
                        st.info("يمكنك استخدام هذا الخيار مجاناً وبدون أي أخطاء من خلال توجيه الملفات لموقع Gemini المباشر وحفظ النتائج هنا.")
                        
                        sess_names = [s.get("session_name", "جلسة") for s in sessions_data]
                        selected_ext_sess = st.selectbox("اختر الجلسة التي تريد تحليلها عبر Gemini Web:", sess_names)
                        
                        prompt_template = (
                            "أنت مدقق ومراجع دقيق لمستندات المشاريع والمخيمات.\n"
                            "قمت بإرفاق ملفات هذه الجلسة لك (كشوف الحضور، تقارير الإنجاز، أوراق التوثيق).\n"
                            "قم بقراءة محتوى جميع الملفات المرفقة واستخرج البيانات التالية بدقة تامة:\n"
                            "1. بيانات كشف الحضور (التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات).\n"
                            "2. بيانات التقرير (التاريخ، الإجمالي، رجال، نساء، أطفال ذكور، فتيات، ذوي احتياجات).\n"
                            "3. قارن بين أرقام الحضور وأرقام التقرير وحدد الفروقات أو حالة التطابق لكل بند.\n\n"
                            "تنبيه: إذا كان أحد الملفين مفقوداً ضع '❌ غير متوفر'.\n"
                            "أجب بصيغة JSON صارمة فقط بالشكل التالي دون أي كود خارجي:\n"
                            "{\n"
                            '  "attendance_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
                            '  "report_data": ["التاريخ", "الإجمالي", "رجال", "نساء", "أطفال ذكور", "فتيات", "ذوي احتياجات"],\n'
                            '  "differences": ["مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق...", "مطابق / فرق..."]\n'
                            "}"
                        )

                        st.markdown("**1️⃣ انسخ هذا الأمر وارفقه مع ملفات الجلسة في موقع [Gemini Web](https://gemini.google.com):**")
                        st.code(prompt_template, language="text")

                        st.markdown("**2️⃣ الصق الرد النامج من موقع Gemini هنا لحفظه في الجدول مباشرة:**")
                        pasted_json = st.text_area("الرد النامج من Gemini (JSON):", height=150)
                        
                        if st.button("💾 حفظ البيانات المستخرجة في التطبيق"):
                            if pasted_json:
                                try:
                                    cleaned_j = pasted_json.replace("```json", "").replace("```", "").strip()
                                    parsed_d = json.loads(cleaned_j)
                                    all_logs = load_scan_logs()
                                    if p_name not in all_logs: all_logs[p_name] = {}
                                    
                                    all_logs[p_name][selected_ext_sess] = {
                                        "attendance": parsed_d.get("attendance_data", []),
                                        "report": parsed_d.get("report_data", []),
                                        "differences": parsed_d.get("differences", []),
                                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                                    }
                                    save_scan_logs(all_logs)
                                    st.success(f"✅ تم حفظ نتائج {selected_ext_sess} بنجاح!")
                                except Exception as ex:
                                    st.error(f"❌ الصيغة الملصقة غير صحيحة: {ex}")

                    elif current_view == "stats":
                        st.markdown("#### 📊 إحصائية المستفيدين استناداً للسجلات المحفوظة:")
                        all_logs = load_scan_logs()
                        project_logs = all_logs.get(p_name, {})

                        tot_men, tot_women, tot_boys, tot_girls, tot_pwd = 0, 0, 0, 0, 0
                        has_data = False

                        for sess in sessions_data:
                            sess_title = sess.get("session_name", "جلسة")
                            log_entry = project_logs.get(sess_title, None)
                            if log_entry:
                                att_v = log_entry.get("attendance", [])
                                if len(att_v) >= 7 and "❌" not in str(att_v):
                                    has_data = True
                                    s_men = extract_number(att_v[2])
                                    s_women = extract_number(att_v[3])
                                    s_boys = extract_number(att_v[4])
                                    s_girls = extract_number(att_v[5])
                                    s_pwd = extract_number(att_v[6])

                                    tot_men += s_men
                                    tot_women += s_women
                                    tot_boys += s_boys
                                    tot_girls += s_girls
                                    tot_pwd += s_pwd

                                    with st.expander(f"📌 \u200e{sess_title}\u200f"):
                                        c1, c2, c3, c4, c5 = st.columns(5)
                                        c1.metric("👨 رجال", str(s_men))
                                        c2.metric("👩 نساء", str(s_women))
                                        c3.metric("👧 فتيات", str(s_girls))
                                        c4.metric("👶 أطفال", str(s_boys))
                                        c5.metric("♿ ذوي الاحتياجات", str(s_pwd))

                        if has_data:
                            st.markdown("---")
                            st.markdown("### 📈 إجمالي المستفيدين للمشروع:")
                            tc1, tc2, tc3, tc4, tc5 = st.columns(5)
                            tc1.metric("👨 إجمالي الرجال", str(tot_men))
                            tc2.metric("👩 إجمالي النساء", str(tot_women))
                            tc3.metric("👧 إجمالي الفتيات", str(tot_girls))
                            tc4.metric("👶 إجمالي الأطفال", str(tot_boys))
                            tc5.metric("♿ إجمالي الاحتياجات", str(tot_pwd))
                        else:
                            st.info("قم بتشغيل 'مطابقة الحضور' أولاً لتجميع الإحصائيات.")

                    elif current_view == "logs":
                        st.markdown("#### 📂 سجل الفحص والمطابقة المحفوظ:")
                        all_logs = load_scan_logs()
                        project_logs = all_logs.get(p_name, {})
                        
                        if not project_logs:
                            st.info("لا توجد سجلات محفوظة لهذا المشروع بعد.")
                        else:
                            for sess_name, log_data in list(project_logs.items()):
                                with st.expander(f"📌 \u200e{sess_name}\u200f (آخر فحص: \u200e{log_data.get('timestamp')}\u200f)"):
                                    st.table({
                                        "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال", "النساء", "الأولاد", "الفتيات", "ذوي الاحتياجات"],
                                        "ورقة الحضور": log_data.get("attendance", []),
                                        "التقرير": log_data.get("report", []),
                                        "الفروقات": log_data.get("differences", [])
                                    })
                                    if st.button(f"🗑️ حذف السجل", key=f"del_log_{p_name}_{sess_name}"):
                                        del all_logs[p_name][sess_name]
                                        save_scan_logs(all_logs)
                                        st.rerun()

                    elif current_view == "gap_analysis":
                        st.markdown("#### 📊 تقرير تحليل الفجوات والمخاطر الشامل:")
                        all_logs = load_scan_logs()
                        project_logs = all_logs.get(p_name, {})

                        if st.button("🚀 توليد تقرير الفجوات بالذكاء الاصطناعي", key=f"gen_gap_{p_name}"):
                            with st.spinner("جاري صياغة تقرير الفجوات والمخاطر..."):
                                gap_context = f"مشروع: {p_name} | المجلد: {current_folder['name']}\n\n"
                                for s in sessions_data:
                                    s_title = s.get('session_name', 'جلسة')
                                    l_entry = project_logs.get(s_title, {})
                                    
                                    gap_context += f"### المسار: {s_title}\n"
                                    gap_context += f"- بيانات الحضور: {l_entry.get('attendance', 'لم تفحص بعد')}\n"
                                    gap_context += f"- بيانات التقرير: {l_entry.get('report', 'لم تفحص بعد')}\n"
                                    gap_context += f"- الفروقات والملاحظات: {l_entry.get('differences', 'لم تفحص بعد')}\n\n"

                                gap_prompt = (
                                    "أنت خبير محترف في المتابعة والتقييم (M&E) لمشاريع الإغاثة والتنمية.\n"
                                    "بناءً على البيانات المرفقة أدناه، قم بإعداد تقرير تحليل فجوات ومخاطر شامل وواضح باللغة العربية.\n\n"
                                    f"بيانات المشروع والجلسات:\n{gap_context}"
                                )

                                try:
                                    genai.configure(api_key=GEMINI_API_KEY.strip())
                                    model = genai.GenerativeModel(selected_model)
                                    gap_res = model.generate_content(gap_prompt)
                                    st.session_state[f"gap_report_{p_name}"] = gap_res.text
                                except Exception as e:
                                    st.error(f"❌ حدث خطأ أثناء توليد التقرير: {e}")

                        if f"gap_report_{p_name}" in st.session_state:
                            st.markdown("---")
                            st.markdown(st.session_state[f"gap_report_{p_name}"])

                    elif current_view == "chat":
                        st.markdown("#### 💬 دردشة المساعد الذكي للمشروع:")
                        all_logs = load_scan_logs()
                        project_logs = all_logs.get(p_name, {})

                        chat_key = f"messages_{p_name}"
                        if chat_key not in st.session_state:
                            st.session_state[chat_key] = []

                        for msg in st.session_state[chat_key]:
                            with st.chat_message(msg["role"]):
                                st.write(msg["content"])

                        if prompt_text := st.chat_input("اسأل المساعد عن الجلسات أو الأعداد أو التقارير..."):
                            st.session_state[chat_key].append({"role": "user", "content": prompt_text})
                            with st.chat_message("user"):
                                st.write(prompt_text)

                            context_text = f"مشروع: {p_name} | عدد الجلسات: {len(sessions_data)}\n\n"
                            for s in sessions_data:
                                s_title = s.get('session_name', '')
                                l_entry = project_logs.get(s_title, {})
                                context_text += f"- {s_title}:\n"
                                context_text += f"  * الحضور: {l_entry.get('attendance', 'غير مفحوص')}\n"
                                context_text += f"  * التقرير: {l_entry.get('report', 'غير مفحوص')}\n"

                            with st.chat_message("assistant"):
                                with st.spinner("جاري التفكير والأجابة..."):
                                    ai_prompt = f"أنت مساعد ذكي للمتابعة والتقييم.\nأجب باللغة العربية بناءً على البيانات التالية:\n{context_text}\nسؤال المستخدم: {prompt_text}"
                                    try:
                                        genai.configure(api_key=GEMINI_API_KEY.strip())
                                        model = genai.GenerativeModel(selected_model)
                                        resp = model.generate_content(ai_prompt)
                                        st.write(resp.text)
                                        st.session_state[chat_key].append({"role": "assistant", "content": resp.text})
                                    except Exception as e:
                                        st.error(f"❌ خطأ: {e}")

st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>", 
    unsafe_allow_html=True
)
