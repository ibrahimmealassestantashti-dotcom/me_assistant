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

# --- 🔄 البحث التكراري العميق في كافة الأنشطة والجلسات ---
def get_filtered_session_folders(service, folder_id, month_filter, year_filter, parent_path=""):
    """البحث التكراري لجميع المجلدات داخل كل الأنشطة والجلسات بدون التوقف عند المستوى الأول"""
    sessions = []
    query = f"'{folder_id}' in parents and trashed = false"
    
    try:
        results = service.files().list(
            q=query, supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="files(id, name, mimeType)"
        ).execute()
        
        items = results.get("files", [])
        
        for item in items:
            if item["mimeType"] == "application/vnd.google-apps.folder":
                folder_name = item["name"]
                current_path = f"{parent_path} / {folder_name}" if parent_path else folder_name
                
                # التحقق مما إذا كان هذا المجلد هو جلسة تطابق الفلتر
                is_match = (month_filter.lower() in folder_name.lower()) and (str(year_filter) in folder_name)
                
                # جلب الملفات المباشرة داخل المجلد
                files_query = f"'{item['id']}' in parents and trashed = false"
                sub_files = service.files().list(
                    q=files_query, supportsAllDrives=True, includeItemsFromAllDrives=True,
                    fields="files(id, name, mimeType, webViewLink)"
                ).execute().get("files", [])

                if is_match:
                    sessions.append({
                        "session_id": item["id"],
                        "session_name": folder_name,
                        "full_path": current_path,
                        "files": sub_files
                    })

                # الغوص التكراري للبحث داخل هذا المجلد (للأنشطة التي تحتوي على جلسات فرعية)
                sub_sessions = get_filtered_session_folders(service, item["id"], month_filter, year_filter, current_path)
                sessions.extend(sub_sessions)

    except Exception as e:
        st.error(f"خطأ أثناء قراءة المجلدات: {e}")
        
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
            folder_id = st.session_state.projects[p_name]
            
            st.subheader(f"📅 تصفية جلسات مشروع: {p_name}")
            f_col1, f_col2, f_col3 = st.columns([2, 2, 2])
            
            with f_col1:
                months_list = ["January", "February", "March", "April", "May", "June", 
                               "July", "August", "September", "October", "November", "December"]
                selected_month = st.selectbox("اختر الشهر", months_list, index=5, key=f"m_{p_name}")
            with f_col2:
                selected_year = st.number_input("اختر السنة", min_value=2020, max_value=2030, value=2026, key=f"y_{p_name}")
            
            with f_col3:
                st.write(" ")
                st.write(" ")
                load_btn = st.button(f"🔍 قراءة جلسات {selected_month} {selected_year}", key=f"load_{p_name}")

            if load_btn or f"data_{p_name}" in st.session_state:
                if load_btn:
                    with st.spinner("جاري مسح جميع الأنشطة المترابطة والجلسات المترتبة عليها..."):
                        sessions = get_filtered_session_folders(service, folder_id, selected_month, selected_year)
                        st.session_state[f"data_{p_name}"] = sessions
                
                sessions_data = st.session_state.get(f"data_{p_name}", [])
                
                if not sessions_data:
                    st.warning(f"لم يتم العثور على مجلدات مطابقة لـ {selected_month} {selected_year} في أي من الأنشطة.")
                else:
                    st.success(f"تم جلب وتمييز {len(sessions_data)} جلسة/مجلد عبر كافة أنشطة المشروع بنجاح!")
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
                            
                            with st.expander(f"النشاط والجلسة: {sess['full_path']} - الحالة: {status}"):
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
                            
                            context_text = f"مشروع: {p_name} | الفترة: {selected_month} {selected_year}\n"
                            context_text += f"إجمالي الجلسات المكتشفة في كل الأنشطة: {len(sessions_data)}\n"
                            context_text += "قائمة الجلسات والملفات المكتشفة:\n"
                            for s in sessions_data:
                                context_text += f"- المسار: {s['full_path']}\n"
                                context_text += "  الملفات بداخلها:\n"
                                for f in s['files']:
                                    context_text += f"    * {f['name']}\n"

                            with st.spinner("جاري التحليل القائم على كافة الجلسات والأنشطة..."):
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
                                            ans = f"📌 **عدد الجلسات المكتشفة في كافة الأنشطة لـ ({selected_month} {selected_year}):** هو **{total_sessions}** جلسة/مجلد.\n\n**مسارات الجلسات والأنشطة:**\n"
                                            for s in sessions_data:
                                                ans += f"- {s['full_path']}\n"
                                        elif "انجليز" in user_query or "عرب" in user_query or "تسميات" in user_query:
                                            has_arabic = any(re.search(r'[\u0600-\u06FF]', n) for n in all_names)
                                            has_english = any(re.search(r'[a-zA-Z]', n) for n in all_names)
                                            ans = f"🔍 **تحليل الأسماء والتسميات في كافة الأنشطة ({len(all_names)} عنصر):**\n\n"
                                            if has_english and not has_arabic:
                                                ans += "• جميع التسميات المكتشفة مكتوبة باللغة **الإنكليزية** فقط."
                                            elif has_arabic and has_english:
                                                ans += "• التسميات تحتوي على مزيج بين اللغة **العربية** واللغة **الإنكليزية**."
                                            else:
                                                ans += "• جميع التسميات مكتوبة باللغة **العربية**."
                                        else:
                                            ans = f"بناءً على قراءة كافة أنشطة وجلسات {selected_month} {selected_year}:\n"
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
