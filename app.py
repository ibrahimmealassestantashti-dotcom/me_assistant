# 1️⃣ فحص المرفقات الصحيحة (Attendance, Documentation, Report)
                    if b1.button("1️⃣ فحص المرفقات للجلسات", key=f"b1_{p_name}"):
                        st.markdown("#### 📋 نتيجة فحص مجلدات الجلسة الثلاثة والمرفقات:")
                        for sess in sessions_data:
                            # الاستخدام الآمن باستخدام .get لمنع KeyError
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
