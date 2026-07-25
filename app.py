import streamlit as st
import json
import os
import time
import random
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai

st.set_page_config(
    page_title="مساعد إدارة ومتابعة المشاريع - ME Assistant",
    page_icon="📊",
    layout="wide"
)

CONFIG_FILE = "saved_projects.json"

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

def call_gemini_api(prompt, api_key):
    """دالة اتصال محدثة لاستخدام نموذج gemini-2.5-flash مع إعادة محاولة تلقائية (Exponential Backoff)"""
    max_retries = 3
    delay = 3
    
    for attempt in range(max_retries):
        try:
            client = genai.Client(api_key=api_key.strip())
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            class MockResponse:
                status_code = 200
                text = response.text
                def json(self):
                    return {
                        'candidates': [{
                            'content': {
                                'parts': [{'text': response.text}]
                            }
                        }]
                    }
            return MockResponse()
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt < max_retries - 1:
                    sleep_time = delay * (2 ** attempt) + random.uniform(0.5, 1.5)
                    time.sleep(sleep_time)
                    continue
                else:
                    class MockErrorResponse:
                        status_code = 429
                        text = err_str
                        def json(self):
                            return {'error': {'message': err_str}}
                    return MockErrorResponse()
            else:
                class MockErrorResponse:
                    status_code = 500
                    text = err_str
                    def json(self):
                        return {'error': {'message': err_str}}
                return MockErrorResponse()

def extract_session_metrics_with_ai(session_info, api_key):
    if "cached_metrics" in session_info:
        return session_info["cached_metrics"]
        
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
    
    default_att = ["--/--/--", "0", "0", "0", "0", "0", "0"]
    default_rep = ["--/--/--", "0", "0", "0", "0", "0", "0"]
    default_diff = ["✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق", "✅ مطابق"]

    try:
        res = call_gemini_api(prompt, api_key)
        time.sleep(1.2)
        
        if res.status_code == 200:
            text_res = res.json()['candidates'][0]['content']['parts'][0]['text']
            cleaned = text_res.replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            session_info["cached_metrics"] = (
                data.get("attendance_data", default_att), 
                data.get("report_data", default_rep), 
                data.get("differences", default_diff)
            )
        elif res.status_code == 429:
            st.warning("⚠️ تم بلوغ الحد الأقصى المؤقت لطلبات الخطة المجانية. تم استخدام القيم التلقائية بانتظار تجدد الحصة.")
            session_info["cached_metrics"] = (default_att, default_rep, default_diff)
        else:
            session_info["cached_metrics"] = (default_att, default_rep, default_diff)
    except Exception as e:
        session_info["cached_metrics"] = (default_att, default_rep, default_diff)
        
    return session_info["cached_metrics"]

def has_session_mismatch(session_info, api_key):
    _, _, diff_vals = extract_session_metrics_with_ai(session_info, api_key)
    return any("⚠️" in d or "خطأ" in d for d in diff_vals)

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
    st.warning("⚠️ الرجاء إدخال مفتاح الذكاء الاصطناعي في الشريط الجانبي من فضلك لكي يعمل التحليل والدردشة الذكية.")

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

            if btn_fetch:
                with st.spinner("جاري تحديد مجلدات الجلسات وفحص المجلدات الفرعية الثلاثة بداخلها..."):
                    sessions = fetch_structured_sessions(service, current_folder["id"])
                    st.session_state[f"data_{p_name}"] = sessions

            sessions_data = st.session_state.get(f"data_{p_name}", None)
            
            if sessions_data is not None:
                if not sessions_data:
                    st.warning("لم يتم العثور على جلسات مكتملة الهيكلية داخل هذا المجلد.")
                else:
                    st.success(f"تم اكتشاف {len(sessions_data)} جلسة/جلسات بالهيكلية الصحيحة!")
                    st.markdown("---")
                    
                    st.subheader("🛠️ أدوات التحليل والمطابقة الذكية")
                    b1, b2, b3, b4 = st.columns(4)
                    
                    if b1.button("1️⃣ فحص المرفقات للجلسات", key=f"b1_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "attachments"
                    if b2.button("2️⃣ مطابقة الحضور والتقرير", key=f"b2_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "matching"
                    if b3.button("3️⃣ إحصائية الفئات والحضور", key=f"b3_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "stats"
                    if b4.button("4️⃣ المساعد الذكي (AI Chat)", key=f"b4_{p_name}"):
                        st.session_state[f"view_{p_name}"] = "chat"

                    current_view = st.session_state.get(f"view_{p_name}", None)

                    if current_view == "attachments":
                        st.markdown("#### 📋 نتيجة فحص مجلدات الجلسات الفرعية الثلاثة:")
                        for sess in sessions_data:
                            att_files = sess.get("attendance", {}).get("files", [])
                            doc_files = sess.get("documentation", {}).get("files", [])
                            rep_files = sess.get("report", {}).get("files", [])
                            
                            is_complete = len(att_files) > 0 and len(doc_files) > 0 and len(rep_files) > 0
                            has_error = has_session_mismatch(sess, GEMINI_API_KEY) if GEMINI_API_KEY else False
                            
                            status_tag = "✅ مكتملة" if is_complete else "⚠️ ناقصة"
                            if has_error:
                                status_tag += " | ⚠️ تنبيه: يوجد خطأ مطابقه"

                            sess_title = sess.get("session_name", "جلسة بدون عنوان")
                            
                            with st.expander(f"📌 **{sess_title}** — الحالة: {status_tag}"):
                                col_f1, col_f2, col_f3 = st.columns(3)
                                with col_f1:
                                    st.write("**📄 ورقة الحضور:**")
                                    for f in att_files: st.caption(f"• {f['name']}")
                                with col_f2:
                                    st.write("**🖼️ صور التوثيق (Documentation):**")
                                    for f in doc_files: st.caption(f"• {f['name']}")
                                with col_f3:
                                    st.write("**📑 التقرير:**")
                                    for f in rep_files: st.caption(f"• {f['name']}")

                    elif current_view == "matching":
                        st.markdown("#### ⚖️ مطابقة أوراق الحضور مع التقارير بالذكاء الاصطناعي:")
                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح Gemini API في الشريط الجانبي أولاً.")
                        else:
                            for sess in sessions_data:
                                sess_title = sess.get("session_name", "جلسة")
                                has_error = has_session_mismatch(sess, GEMINI_API_KEY)
                                title_prefix = "⚠️ [يوجد خطأ مطابقة] " if has_error else ""
                                
                                att_vals, rep_vals, diff_vals = extract_session_metrics_with_ai(sess, GEMINI_API_KEY)
                                comparison_data = {
                                    "البند": ["تاريخ الجلسة", "العدد الإجمالي", "الرجال (Men)", "النساء (Women)", "الأولاد (Boys)", "الفتيات (Girls)", "ذوي الاحتياجات (PWD)"],
                                    "ورقة الحضور": att_vals,
                                    "التقرير": rep_vals,
                                    "النتيجة / الفروقات": diff_vals
                                }
                                with st.expander(f"🔍 {title_prefix}**جلسة: {sess_title}**"):
                                    st.table(comparison_data)

                    elif current_view == "stats":
                        st.markdown("#### 📊 الإحصائية التجميعية للمستفيدين عبر كافة الجلسات:")
                        if GEMINI_API_KEY:
                            tot_boys = sum(int(extract_session_metrics_with_ai(s, GEMINI_API_KEY)[0][4]) for s in sessions_data)
                            tot_girls = sum(int(extract_session_metrics_with_ai(s, GEMINI_API_KEY)[0][5]) for s in sessions_data)
                            tot_pwd = sum(int(extract_session_metrics_with_ai(s, GEMINI_API_KEY)[0][6]) for s in sessions_data)
                        else:
                            tot_boys, tot_girls, tot_pwd = 0, 0, 0
                            
                        col_stat1, col_stat2, col_stat3, col_stat4, col_stat5 = st.columns(5)
                        col_stat1.metric("👨 رجال", "0")
                        col_stat2.metric("👩 نساء", "0")
                        col_stat3.metric("👧 فتيات إناث", str(tot_girls))
                        col_stat4.metric("👶 أطفال ذكور", str(tot_boys))
                        col_stat5.metric("♿ ذوي الاحتياجات", str(tot_pwd))

                    elif current_view == "chat":
                        st.markdown("---")
                        st.subheader("💬 دردشة المساعد الذكي لمراجعة الجلسات")

                        if not GEMINI_API_KEY:
                            st.error("يرجى إدخال مفتاح الذكاء الاصطناعي في الشريط الجانبي لتفعيل المحادثة.")
                        else:
                            chat_history_key = f"messages_{p_name}"
                            if chat_history_key not in st.session_state:
                                st.session_state[chat_history_key] = []

                            for message in st.session_state[chat_history_key]:
                                with st.chat_message(message["role"]):
                                    st.write(message["content"])

                            if prompt_text := st.chat_input("وجه سؤالك للذكاء الاصطناعي حول بيانات وقراءات الجلسات..."):
                                st.session_state[chat_history_key].append({"role": "user", "content": prompt_text})
                                with st.chat_message("user"):
                                    st.write(prompt_text)

                                context_text = f"مشروع: {p_name} | المجلد: {current_folder['name']}\n"
                                context_text += f"عدد الجلسات الإجمالي: {len(sessions_data)}\n\n"
                                
                                tot_pwd_all, tot_boys_all, tot_girls_all = 0, 0, 0
                                for s in sessions_data:
                                    s_title = s.get('session_name', '')
                                    att_v, rep_v, _ = extract_session_metrics_with_ai(s, GEMINI_API_KEY)
                                    tot_pwd_all += int(att_v[6])
                                    tot_boys_all += int(att_v[4])
                                    tot_girls_all += int(att_v[5])
                                    context_text += f"- {s_title}:\n"
                                    context_text += f"  * التواريخ: حضور ({att_v[0]})، تقرير ({rep_v[0]})\n"
                                    context_text += f"  * المجموع: حضور ({att_v[1]})، تقرير ({rep_v[1]})\n"
                                    context_text += f"  * التفاصيل: أولاد ({att_v[4]})، فتيات ({att_v[5]})، ذوي احتياجات ({att_v[6]})\n"

                                context_text += f"\nالإجمالي الكلي:\n- ذوي الاحتياجات (PWD): {tot_pwd_all}\n- الأولاد: {tot_boys_all}\n- الفتيات: {tot_girls_all}\n"

                                with st.chat_message("assistant"):
                                    with st.spinner("جاري معالجة السؤال بواسطة الذكاء الاصطناعي..."):
                                        try:
                                            ai_prompt = f"أنت مساعد ذكي لإدارة المشاريع ومتابعة الجلسات.\nاجب بلغة عربية دقيقة وبشكل مباشر بأرقام وإحصائيات بناءً على البيانات التالية:\n\n--- البيانات ---\n{context_text}\n--- نهاية البيانات ---\n\nسؤال المستخدم: {prompt_text}"
                                            
                                            res = call_gemini_api(ai_prompt, GEMINI_API_KEY)
                                            res_json = res.json()

                                            if res.status_code == 200:
                                                ai_response = res_json['candidates'][0]['content']['parts'][0]['text']
                                                st.write(ai_response)
                                                st.session_state[chat_history_key].append({"role": "assistant", "content": ai_response})
                                            else:
                                                err_msg = res_json.get('error', {}).get('message', 'خطأ غير معروف')
                                                st.error(f"❌ خطأ من Google API: {err_msg}")
                                                
                                        except Exception as e:
                                            st.error(f"❌ فشل الاتصال بالخادم: {e}")

st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; padding: 10px;'>"
    "تم تصميم وتطوير البرنامج بواسطة <b>إبراهيم الجاسم</b> © 2026"
    "</div>", 
    unsafe_allow_html=True
)
