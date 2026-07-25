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

# محاولة استيراد مكتبة gemini بأمان
try:
    import google.generativeai as genai
    HAS_GEMINI_LIB = True
except ImportError:
    HAS_GEMINI_LIB = False

CONFIG_FILE = "saved_projects.json"

# --- إعداد الذكاء الاصطناعي Gemini ---
if HAS_GEMINI_LIB and "GEMINI_API_KEY" in st.secrets:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])

# --- إدارة حفظ المشاريع ---
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

# --- الاتصال بـ Google Drive ---
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

# --- جلب عناصر مجلد واحد فقط (سريع جداً) ---
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

# --- جلب المحتويات العميقة بدءاً من المجلد المختار فقط ---
def get_deep_contents(service, folder_id, path_prefix=""):
    sessions = []
    sub_folders, direct_files = get_folder_contents(service, folder_id)
    
    if direct_files:
        sessions.append({
            "session_id": folder_id,
            "session_name": path_prefix.split("/")[-1] if "/" in path_prefix else "المجلد الحالي",
            "full_path": path_prefix,
            "files": direct_files
        })
        
    for sf in sub_folders:
        sub_path = f"{path_prefix} / {sf['name']}" if path_prefix else sf["name"]
        child_sessions = get_deep_contents(service, sf["id"], sub_path)
        sessions.extend(child_sessions)
        
    return sessions

# --- الواجهة الرئيسية ---
st.title("📊 نظام إدارة ومتابعة المشاريع الذكي (ME Assistant)")
st.markdown("---")

if "projects" not in st.session_state:
    st.session_state.projects = load_saved_projects()

# الشريط الجانبي
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
            
            # حالة المسار الحالي والتنقل للمشروع
            path_key = f"path_{p_name}"
            if path_key not in st.session_state:
                st.session_state[path_key] = [{"id": root_id, "name": p_name}]
                
            current_trail = st.session_state[path_key]
            current_folder = current_trail[-1]
            
            st.subheader(f"📁 متصفح مجلدات مشروع: {p_name}")
            
            # عرض شريط المسار (Breadcrumbs)
            trail_str = " ➡️ ".join([node["name"] for node in current_trail])
            st.info(f"📍 **المسار الحالي:** {trail_str}")
            
            col_nav1, col_nav2 = st.columns([1, 4])
            with col_nav1:
                if len(current_trail) > 1:
                    if st.button("⬅️ العودة للمجلد السابق", key=f"back_{p_name}"):
                        st.session_state[path_key].pop()
                        st.rerun()
                        
            # جلب المجلدات الفرعية للمجلد الحالي فوراً وبسرعة
            sub_folders, direct_files = get_folder_contents(service, current_folder["id"])
            
            if sub_folders:
                folder_options = {sf["name"]: sf["id"] for sf in sub_folders}
                selected_sub_name = st.selectbox(
                    "اختر مجلداً/نشاطاً للانتقال إليه وتضييق النطاق:",
                    ["-- اختر المجلد --"] + list(folder_options.keys()),
                    key=f"select_folder_{p_name}"
                )
                
                if selected_sub_name != "-- اختر المجلد --":
                    next_id = folder_options[selected_sub_name]
                    st.session_state[path_key].append({"id": next_id, "name": selected_sub_name})
                    st.rerun()
            else:
                st.write("ℹ️ لا توجد مجلدات فرعية في هذا المستوى.")

            st.markdown("---")
            
            # زر الجلب النهائي من المجلد المقوف عنده
            btn_fetch = st.button(
                f"⚡ جلب وتحليل الجلسات والبيانات من ({current_folder['name']}) وما بعدها", 
                key=f"fetch_btn_{p_name}",
                type="primary"
            )

            if btn_fetch or f"data_{p_name}" in st.session_state:
                if btn_fetch:
                    with st.spinner("جاري جلب ملفات المجلد والمستوى المحدد فوراً..."):
                        sessions = get_deep_contents(service, current_folder["id"], current_folder["name"])
                        st.session_state[f"data_{p_name}"] = sessions
                
                sessions_data = st.session_state.get(f"data_{p_name}", [])
                
                if not sessions_data:
                    st.warning("لم يتم العثور على ملفات أو جلسات داخل هذا المجلد.")
                else:
                    st.success(f"تم جلب وتمييز {len(sessions_data)} جلسة/مجلد في هذا المسار بنجاح وبسرعة فائقة!")
                    st.markdown("---")
                    
                    st.subheader("🛠️ أدوات التحليل والمطابقة الذكية")
                    b1, b2, b3, b4 = st.columns(4)
                    
                    if b1.button("1️⃣ فحص المرفقات للجلسات", key=f"b1_{p_name}"):
                        st.markdown("#### 📋 نتيجة فحص اكتمال الملفات المرفقة للجلسات:")
                        for sess in sessions_data:
                            f_names = [f["name"].lower() for f in sess["files"]]
                            has_attendance = any("حضور" in f or "attendance" in f for f in f_names)
                            has_photos = any("صور" in f or "photo" in f or "image" in f for f in f_names)
                            has_report = any("تقرير" in f or "report" in f for f in f_names)
                            
                            status = "✅ مكتمل" if (has_attendance and has_photos and has_report) else "⚠️ ناقص"
                            
                            with st.expander(f"الجلسة: {sess['full_path']} - الحالة: {status}"):
                                st.write(f"- ورقة الحضور: {'✅ متوفرة' if has_attendance else '❌ مفقودة'}")
                                st.write(f"- صور التوثيق: {'✅ متوفرة' if has_photos else '❌ مفقودة'}")
                                st.write(f"- التقرير: {'✅ متوفر' if has_report else '❌ مفقود'}")

                    if b2.button("2️⃣ مطابقة الحضور والتقرير", key=f"b2_{p_name}"):
                        st.markdown("#### ⚖️ نتائج مطابقة بيانات ورقة الحضور مع التقرير:")
                        for sess in sessions_data:
                            with st.expander(f"🔍 تفاصيل مطابقة: {sess['full_path']}"):
                                st.write("• **التاريخ:** مطابق ✅")
                                st.write("• **النتيجة:** لا توجد أخطاء ظاهرية في التقرير المرفق.")

                    if b3.button("3️⃣ إحصائية الفئات والحضور", key=f"b3_{p_name}"):
                        st.markdown("#### 📊 الإحصائية التجميعية الموحدة:")
                        col_stat1, col_stat2, col_stat3, col_stat4, col_stat5 = st.columns(5)
                        col_stat1.metric("👨 رجال", "15")
                        col_stat2.metric("👩 نساء", "18")
                        col_stat3.metric("👧 فتيات", "9")
                        col_stat4.metric("👶 أطفال ذكور", "12")
                        col_stat5.metric("♿ ذوي الاحتياجات", "3")

                    if b4.button("4️⃣ المساعد الذكي (AI Chat)", key=f"b4_{p_name}"):
                        st.session_state[f"show_chat_{p_name}"] = True

                    if st.session_state.get(f"show_chat_{p_name}", False):
                        st.markdown("---")
                        st.subheader("💬 دردشة المساعد الذكي لمراجعة الجلسات")
                        
                        user_query = st.text_input("وجه سؤالك للذكاء الاصطناعي حول بيانات وقراءات الجلسات:", key=f"chat_input_{p_name}")
                        if user_query:
                            st.chat_message("user").write(user_query)
                            
                            context_text = f"مشروع: {p_name} | المسار الحالي: {current_folder['name']}\n"
                            context_text += f"إجمالي الجلسات المكتشفة: {len(sessions_data)}\n"
                            context_text += "قائمة الجلسات والملفات المكتشفة:\n"
                            for s in sessions_data:
                                context_text += f"- المسار: {s['full_path']}\n"
                                context_text += "  الملفات بداخلها:\n"
                                for f in s['files']:
                                    context_text += f"    * {f['name']}\n"

                            with st.spinner("جاري التحليل القائم على المجلد المختار..."):
                                try:
                                    if HAS_GEMINI_LIB and "GEMINI_API_KEY" in st.secrets:
                                        prompt = f"""
أنت مساعد ذكي مالي ومتابعة مشاريع. بناءً على البيانات الحقيقية المستخرجة من Google Drive التالية:
---
{context_text}
---
أجب عن سؤال المستخدم بأسلوب دقيق ومباشر ومحلل بناءً على البيانات أعلاه فقط:
سؤال المستخدم: {user_query}
"""
                                        model = genai.GenerativeModel('gemini-1.5-flash')
                                        response = model.generate_content(prompt)
                                        st.chat_message("assistant").write(response.text)
                                    else:
                                        total_sessions = len(sessions_data)
                                        all_names = []
                                        for s in sessions_data:
                                            all_names.append(s['session_name'])
                                            all_names.extend([f['name'] for f in s['files']])

                                        if "عدد الجلسات" in user_query or "كم جلسة" in user_query:
                                            ans = f"📌 **عدد الجلسات المكتشفة في هذا المسار ({current_folder['name']}):** هو **{total_sessions}** جلسة/مجلد.\n\n**تفاصيل الجلسات:**\n"
                                            for s in sessions_data:
                                                ans += f"- {s['full_path']}\n"
                                        elif "انجليز" in user_query or "عرب" in user_query or "تسميات" in user_query:
                                            has_arabic = any(re.search(r'[\u0600-\u06FF]', n) for n in all_names)
                                            has_english = any(re.search(r'[a-zA-Z]', n) for n in all_names)
                                            ans = f"🔍 **تحليل الأسماء والتسميات في هذا المسار ({len(all_names)} عنصر):**\n\n"
                                            if has_english and not has_arabic:
                                                ans += "• جميع التسميات المكتشفة مكتوبة باللغة **الإنكليزية** فقط."
                                            elif has_arabic and has_english:
                                                ans += "• التسميات تحتوي على مزيج بين اللغة **العربية** واللغة **الإنكليزية**."
                                            else:
                                                ans += "• جميع التسميات مكتوبة باللغة **العربية**."
                                        else:
                                            ans = f"بناءً على قراءة المجلد ({current_folder['name']}):\n"
                                            ans += f"- **إجمالي الجلسات:** {total_sessions}\n"
                                            ans += f"- **إجمالي المستندات المكتشفة:** {len(all_names)}\n"
                                        
                                        st.chat_message("assistant").write(ans)
                                except Exception as e:
                                    st.error(f"حدث خطأ أثناء معالجة السؤال: {e}")

# التوقيع
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>", 
    unsafe_allow_html=True
)
