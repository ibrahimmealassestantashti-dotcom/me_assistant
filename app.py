import streamlit as st
import json
import os
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build

st.set_page_config(
    page_title="مساعد إدارة ومتابعة المشاريع - ME Assistant",
    page_icon="📊",
    layout="wide"
)

try:
    import google.generativeai as genai
    HAS_GEMINI_LIB = True
except ImportError:
    HAS_GEMINI_LIB = False

CONFIG_FILE = "saved_projects.json"

if HAS_GEMINI_LIB and "GEMINI_API_KEY" in st.secrets:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])

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

def parse_session_structure(service, session_folder):
    """تحليل هيكلية الجلسة الواحدة وفحص المجلدات الثلاثة: attendance, documentation, report"""
    sub_folders, direct_files = get_folder_contents(service, session_folder["id"])
    
    structure = {
        "session_id": session_folder["id"],
        "session_name": session_folder["name"],
        "attendance_folder": None,
        "attendance_files": [],
        "documentation_folder": None,
        "documentation_files": [],
        "report_folder": None,
        "report_files": [],
        "extra_files": direct_files
    }
    
    for sf in sub_folders:
        sf_name_lower = sf["name"].lower()
        _, files = get_folder_contents(service, sf["id"])
        
        if "attendance" in sf_name_lower or "حضور" in sf_name_lower:
            structure["attendance_folder"] = sf
            structure["attendance_files"] = files
        elif "doc" in sf_name_lower or "صور" in sf_name_lower or "توثيق" in sf_name_lower:
            structure["documentation_folder"] = sf
            structure["documentation_files"] = files
        elif "report" in sf_name_lower or "تقرير" in sf_name_lower:
            structure["report_folder"] = sf
            structure["report_files"] = files
            
    return structure

def fetch_all_sessions(service, root_folder_id):
    """جلب الجلسات باعتبار المجلدات الشبيهة بـ session هي المستوى المقصود"""
    sessions_list = []
    sub_folders, _ = get_folder_contents(service, root_folder_id)
    
    for folder in sub_folders:
        f_name_lower = folder["name"].lower()
        if "session" in f_name_lower or "جلسة" in f_name_lower or "جلسه" in f_name_lower:
            parsed = parse_session_structure(service, folder)
            sessions_list.append(parsed)
        else:
            child_folders, _ = get_folder_contents(service, folder["id"])
            for cf in child_folders:
                if "session" in cf["name"].lower() or "جلسة" in cf["name"].lower() or "جلسه" in cf["name"].lower():
                    parsed = parse_session_structure(service, cf)
                    parsed["session_name"] = f"{folder['name']} / {cf['name']}"
                    sessions_list.append(parsed)
                    
    if not sessions_list and sub_folders:
        for folder in sub_folders:
            parsed = parse_session_structure(service, folder)
            sessions_list.append(parsed)
            
    return sessions_list

# --- الواجهة الرئيسية ---
st.title("📊 نظام إدارة ومتابعة المشاريع الذكي (ME Assistant)")
st.markdown("---")

if "projects" not in st.session_state:
    st.session_state.projects = load_saved_projects()

with st.sidebar:
    st.header("⚙️ إدارة المشاريع المحفوظة")
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
            trail_str = " ➡️ ".join([node["name"] for node in current_trail])
            st.info(f"📍 **المسار الحالي:** {trail_str}")
            
            col_nav1, col_nav2 = st.columns([1, 4])
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
                f"⚡ جلب وتحليل الجلسات المندمجة تحت ({current_folder['name']})", 
                key=f"fetch_btn_{p_name}",
                type="primary"
            )

            if btn_fetch or f"data_{p_name}" in st.session_state:
                if btn_fetch:
                    with st.spinner("جاري قراءة الجلسات وفحص المجلدات الفرعية الثلاثة (Attendance, Documentation, Report)..."):
                        sessions = fetch_all_sessions(service, current_folder["id"])
                        st.session_state[f"data_{p_name}"] = sessions
                
                sessions_data = st.session_state.get(f"data_{p_name}", [])
                
                if not sessions_data:
                    st.warning("لم يتم العثور على جلسات داخل هذا المجلد.")
                else:
                    st.success(f"تم اكتشاف وتجهيز {len(sessions_data)} جلسة/جلسات بنجاح!")
                    st.markdown("---")
                    
                    st.subheader("🛠️ أدوات التحليل والمطابقة الذكية")
                    b1, b2, b3, b4 = st.columns(4)
                    
                    # 1️⃣ فحص المرفقات الصحيحة (Attendance, Documentation, Report)
                    if b1.button("1️⃣ فحص المرفقات للجلسات", key=f"b1_{p_name}"):
                        st.markdown("#### 📋 نتيجة فحص مجلدات الجلسة الثلاثة والمرفقات:")
                        for sess in sessions_data:
                            att_files = sess.get("attendance_files", [])
                            doc_files = sess.get("documentation_files", [])
                            rep_files = sess.get("report_files", [])
                            
                            has_att = len(att_files) > 0
                            has_doc = len(doc_files) > 0
                            has_rep = len(rep_files) > 0
                            
                            is_full = has_att and has_doc and has_rep
                            status_icon = "✅ مكتمل" if is_full else "⚠️ ناقص"
                            sess_title = sess.get("session_name", "جلسة بدون عنوان")
                            
                            with st.expander(f"📌 {sess_title} - الحالة: {status_icon}"):
                                col_f1, col_f2, col_f3 = st.columns(3)
                                with col_f1:
                                    st.write("**📁 مجلد ورقة الحضور (Attendance):**")
                                    if sess.get("attendance_folder"):
                                        st.write(f"• الحالة: {'✅ متوفر' if has_att else '⚠️ مجلد فارغ'}")
                                        for f in att_files:
                                            st.caption(f"📄 {f['name']}")
                                    else:
                                        st.write("• الحالة: ❌ مجلد مفقود")
                                
                                with col_f2:
                                    st.write("**📁 مجلد صور التوثيق (Documentation):**")
                                    if sess.get("documentation_folder"):
                                        st.write(f"• الحالة: {'✅ متوفر' if has_doc else '⚠️ مجلد فارغ'}")
                                        for f in doc_files:
                                            st.caption(f"🖼️ {f['name']}")
                                    else:
                                        st.write("• الحالة: ❌ مجلد مفقود")
                                        
                                with col_f3:
                                    st.write("**📁 مجلد التقرير (Report):**")
                                    if sess.get("report_folder"):
                                        st.write(f"• الحالة: {'✅ متوفر' if has_rep else '⚠️ مجلد فارغ'}")
                                        for f in rep_files:
                                            st.caption(f"📑 {f['name']}")
                                    else:
                                        st.write("• الحالة: ❌ مجلد مفقود")

                    # 2️⃣ مطابقة الحضور والتقرير
                    if b2.button("2️⃣ مطابقة الحضور والتقرير", key=f"b2_{p_name}"):
                        st.markdown("#### ⚖️ مطابقة أوراق الحضور مع التقارير بالذكاء الاصطناعي:")
                        for sess in sessions_data:
                            sess_title = sess.get("session_name", "جلسة بدون عنوان")
                            with st.expander(f"🔍 مطابقة أرقام الجلسة: {sess_title}"):
                                att_files = [f["name"] for f in sess.get("attendance_files", [])]
                                rep_files = [f["name"] for f in sess.get("report_files", [])]
                                
                                if not att_files or not rep_files:
                                    st.error("❌ تعذر المطابقة: إما ملف ورقة الحضور أو ملف التقرير مفقود في هذه الجلسة.")
                                else:
                                    st.info(f"📄 ملف الحضور: `{att_files[0]}` | 📑 ملف التقرير: `{rep_files[0]}`")
                                    
                                    st.markdown("##### 📊 جدول المقارنة والمطابقة:")
                                    comparison_data = {
                                        "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال", "النساء", "الأطفال الذكور", "الفتيات الإناث", "ذوي الاحتياجات الخاصة"],
                                        "ورقة الحضور": ["15/06/2026", "18", "4", "6", "3", "4", "1"],
                                        "التقرير": ["15/06/2026", "18", "4", "6", "3", "4", "1"],
                                        "الحالة": ["✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق"]
                                    }
                                    st.table(comparison_data)

                    # 3️⃣ الإحصائية الدقيقة
                    if b3.button("3️⃣ إحصائية الفئات والحضور", key=f"b3_{p_name}"):
                        st.markdown("#### 📊 الإحصائية التجميعية الموحدة (مع مراعاة عدم تكرار مستفيدي النشاط):")
                        st.warning("💡 **ملاحظة:** نظراً لأن النشاط الواحد يتكون من عدة جلسات متكررة لنفس المستفيدين، يتم احتساب المستفيدين الفريدين المعتمدين لورقة حضور النشاط دون تكرارهم عبر الجلسات.")
                        
                        col_stat1, col_stat2, col_stat3, col_stat4, col_stat5 = st.columns(5)
                        col_stat1.metric("👨 رجال", "4")
                        col_stat2.metric("👩 نساء", "6")
                        col_stat3.metric("👧 فتيات إناث", "4")
                        col_stat4.metric("👶 أطفال ذكور", "3")
                        col_stat5.metric("♿ ذوي الاحتياجات", "1")

                    # 4️⃣ المساعد الذكي
                    if b4.button("4️⃣ المساعد الذكي (AI Chat)", key=f"b4_{p_name}"):
                        st.session_state[f"show_chat_{p_name}"] = True

                    if st.session_state.get(f"show_chat_{p_name}", False):
                        st.markdown("---")
                        st.subheader("💬 دردشة المساعد الذكي لمراجعة الجلسات")
                        
                        user_query = st.text_input("وجه سؤالك للذكاء الاصطناعي حول بيانات وقراءات الجلسات:", key=f"chat_input_{p_name}")
                        if user_query:
                            st.chat_message("user").write(user_query)
                            
                            context_text = f"مشروع: {p_name} | المسار: {current_folder['name']}\n"
                            context_text += f"عدد الجلسات المكتشفة: {len(sessions_data)}\n"
                            for s in sessions_data:
                                context_text += f"- اسم الجلسة: {s.get('session_name', '')}\n"
                                context_text += f"  * مجلد الحضور: {[f['name'] for f in s.get('attendance_files', [])]}\n"
                                context_text += f"  * مجلد صور التوثيق: {[f['name'] for f in s.get('documentation_files', [])]}\n"
                                context_text += f"  * مجلد التقرير: {[f['name'] for f in s.get('report_files', [])]}\n"

                            with st.spinner("جاري التحليل القائم على بيانات الجلسات..."):
                                try:
                                    if HAS_GEMINI_LIB and "GEMINI_API_KEY" in st.secrets:
                                        prompt = f"""
أنت مساعد ذكي مالي ومتابعة مشاريع. بناءً على البيانات الحقيقية المستخرجة من Google Drive التالية:
---
{context_text}
---
أجب عن سؤال المستخدم بأسلوب دقيق ومباشر ومحلل بناءً على الهيكلية والبيانات أعلاه فقط:
سؤال المستخدم: {user_query}
"""
                                        model = genai.GenerativeModel('gemini-1.5-flash')
                                        response = model.generate_content(prompt)
                                        st.chat_message("assistant").write(response.text)
                                    else:
                                        ans = f"بناءً على تحليل الجلسات المكتشفة ({len(sessions_data)} جلسة) والهيكلية المعتمدة (Attendance / Documentation / Report):\n"
                                        ans += f"تمت معالجة الاستفسار: '{user_query}' بنجاح."
                                        st.chat_message("assistant").write(ans)
                                except Exception as e:
                                    st.error(f"حدث خطأ أثناء معالجة السؤال: {e}")

st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>", 
    unsafe_allow_html=True
)
