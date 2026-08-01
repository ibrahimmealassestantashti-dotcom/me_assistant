import streamlit as st
import google.generativeai as genai
import os
import json
import re
import tempfile
import io
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

# --- إعدادات الصفحة ---
st.set_page_config(page_title="مدقق الجلسات", layout="wide", page_icon="⚖️")

# --- دوال جوجل درايف ---
def authenticate_drive():
    # استبدل هذا المسار بمسار ملف الاعتماد (credentials.json) الخاص بك
    # أو استخدم st.secrets إذا كنت ترفع التطبيق على Streamlit Cloud
    creds = service_account.Credentials.from_service_account_file(
        'credentials.json', scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return build('drive', 'v3', credentials=creds)

def download_file_bytes(service, file_id):
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return fh.getvalue()

# --- دالة الذكاء الاصطناعي لاستخراج البيانات ---
def extract_session_metrics_with_ai(service, session_data, api_key, model_name):
    if not api_key:
        return ["خطأ مفتاح API"]*7, ["خطأ مفتاح API"]*7, ["خطأ مفتاح API"]*7

    clean_key = api_key.strip()
    genai.configure(api_key=clean_key)
    
    att_files = session_data.get("attendance", {}).get("files", [])
    rep_files = session_data.get("report", {}).get("files", [])
    
    if not att_files or not rep_files:
        return ["-"]*7, ["-"]*7, ["ملفات ناقصة"]*7

    uploaded_gemini_files = []
    try:
        # 1. معالجة وتجهيز ورقة الحضور
        att_f = att_files[0]
        att_bytes = download_file_bytes(service, att_f["id"])
        suffix_att = os.path.splitext(att_f["name"])[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix_att) as tmp_att:
            tmp_att.write(att_bytes)
            tmp_att_path = tmp_att.name
        
        # 2. معالجة وتجهيز التقرير
        rep_f = rep_files[0]
        rep_bytes = download_file_bytes(service, rep_f["id"])
        suffix_rep = os.path.splitext(rep_f["name"])[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix_rep) as tmp_rep:
            tmp_rep.write(rep_bytes)
            tmp_rep_path = tmp_rep.name
            
        client = genai.Client(api_key=clean_key) if hasattr(genai, "Client") else None
        
        # 3. رفع الملفات إلى Gemini
        if client and hasattr(client, "files"):
            g_att = client.files.upload(file=tmp_att_path, config={"display_name": "Attendance"})
            g_rep = client.files.upload(file=tmp_rep_path, config={"display_name": "Report"})
        else:
            g_att = genai.upload_file(tmp_att_path, display_name="Attendance")
            g_rep = genai.upload_file(tmp_rep_path, display_name="Report")
            
        uploaded_gemini_files.extend([g_att, g_rep])
        
        os.remove(tmp_att_path)
        os.remove(tmp_rep_path)
        
        # 4. توجيه الطلب الدقيق للنموذج (مع التركيز على الخط اليدوي والمسح الضوئي)
        prompt = """
        أنت مدقق بيانات مشاريع دقيق. قمت برفع ملفين لك: 
        الأول: كشف حضور (وهو ملف PDF مسحوب ضوئياً ومكتوب بخط اليد، يرجى التدقيق بعناية في الأرقام).
        الثاني: تقرير الجلسة (ملف رقمي).
        
        المطلوب هو استخراج البيانات التالية من كلا الملفين بدقة للمطابقة:
        1. تاريخ الجلسة
        2. الإجمالي
        3. رجال (Men)
        4. نساء (Women)
        5. أطفال ذكور (Boys)
        6. فتيات إناث (Girls)
        7. ذوي الاحتياجات الخاصة (PWD)

        أجب بصيغة JSON صارمة فقط كالتالي، بدون أي نصوص إضافية:
        {
            "attendance": ["التاريخ", "الإجمالي", "رجال", "نساء", "أولاد", "فتيات", "ذوي الاحتياجات"],
            "report": ["التاريخ", "الإجمالي", "رجال", "نساء", "أولاد", "فتيات", "ذوي الاحتياجات"],
            "differences": ["تطابق/اختلاف", "الفرق الإجمالي", "الفرق", "الفرق", "الفرق", "الفرق", "الفرق"]
        }
        ملاحظة هامة: إذا لم تجد قيمة في الملف ضع "0".
        """
        
        model = genai.GenerativeModel(model_name)
        response = model.generate_content([prompt] + uploaded_gemini_files)
        
        for g_file in uploaded_gemini_files:
            try:
                if hasattr(g_file, "name"): genai.delete_file(g_file.name)
            except: pass
                
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            return [str(x) for x in data.get("attendance", [])], [str(x) for x in data.get("report", [])], [str(x) for x in data.get("differences", [])]
        else:
            return ["فشل القراءة"]*7, ["فشل القراءة"]*7, ["فشل القراءة"]*7
            
    except Exception as e:
        for g_file in uploaded_gemini_files:
            try:
                if hasattr(g_file, "name"): genai.delete_file(g_file.name)
            except: pass
        return ["حدث خطأ برمجي"]*7, ["-"]*7, [f"التفاصيل: {e}"]*7

# --- الواجهة الرئيسية ---
def main():
    st.sidebar.title("الإعدادات")
    user_gemini_key = st.sidebar.text_input("مفتاح Gemini API", type="password")
    final_model_to_use = st.sidebar.selectbox("اختر النموذج", ["gemini-1.5-flash", "gemini-1.5-pro"])
    
    st.title("⚖️ مطابقة أوراق الحضور مع التقارير (الاستخراج الآلي)")
    st.write("سيتم قراءة البيانات ومطابقتها آلياً دون الحاجة للإدخال اليدوي.")

    # محاكاة لبيانات الجلسة (في كودك الفعلي ستجلب هذا من Google Drive)
    # الهيكل يعتمد على مجلد الجلسة وبداخله 3 مجلدات للملفات
    mock_session_data = {
        "attendance": {"files": [{"id": "YOUR_ATTENDANCE_FILE_ID", "name": "attendance_sheet.pdf"}]},
        "report": {"files": [{"id": "YOUR_REPORT_FILE_ID", "name": "session_report.docx"}]}
    }

    st.subheader("📌 جلسة: Session 19")
    
    if st.button("🔍 مطابقة الجلسة", key="match_btn_19"):
        if not user_gemini_key:
            st.error("الرجاء إدخال مفتاح Gemini API في الشريط الجانبي.")
            return
            
        with st.spinner("جاري تحليل المستندات ومطابقة البيانات..."):
            try:
                service = authenticate_drive()
                att_v, rep_v, diff_v = extract_session_metrics_with_ai(
                    service, mock_session_data, user_gemini_key, final_model_to_use
                )
                
                # عرض النتائج في جدول مباشر
                st.success("تم الانتهاء من التحليل!")
                
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.markdown("### بيانات كشف الحضور (PDF مسحوب ضوئياً)")
                    st.write(f"التاريخ: {att_v[0]}")
                    st.write(f"الإجمالي: {att_v[1]}")
                    st.write(f"رجال: {att_v[2]} | نساء: {att_v[3]}")
                    st.write(f"أولاد: {att_v[4]} | فتيات: {att_v[5]}")
                    st.write(f"ذوي الاحتياجات: {att_v[6]}")

                with col2:
                    st.markdown("### بيانات التقرير")
                    st.write(f"التاريخ: {rep_v[0]}")
                    st.write(f"الإجمالي: {rep_v[1]}")
                    st.write(f"رجال: {rep_v[2]} | نساء: {rep_v[3]}")
                    st.write(f"أولاد: {rep_v[4]} | فتيات: {rep_v[5]}")
                    st.write(f"ذوي الاحتياجات: {rep_v[6]}")

                with col3:
                    st.markdown("### الفروقات (المطابقة)")
                    st.write(f"حالة التاريخ: {diff_v[0]}")
                    st.write(f"الفرق الإجمالي: {diff_v[1]}")
                    st.write(f"فرق رجال: {diff_v[2]} | فرق نساء: {diff_v[3]}")
                    st.write(f"فرق أولاد: {diff_v[4]} | فرق فتيات: {diff_v[5]}")
                    st.write(f"فرق ذوي الاحتياجات: {diff_v[6]}")
                    
            except Exception as e:
                st.error(f"حدث خطأ أثناء الاتصال بجوجل درايف: {e}")

if __name__ == "__main__":
    main()
