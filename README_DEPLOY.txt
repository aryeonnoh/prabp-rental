프라비 Streamlit Cloud 배포 묶음 v2

수정 내용
- 검색 결과가 줄었는데 이전 페이지 번호가 남아 있을 때 발생하던
  PGRST103(Requested range not satisfiable) 오류 수정
- 제품 조회, 견적서 만들기, 견적 상세의 상품 추가 팝업에서
  검색어 변경 시 페이지를 자동으로 1페이지로 초기화

GitHub 저장소에 올릴 파일
- app.py
- requirements.txt
- .gitignore
- README_DEPLOY.txt (선택)

절대 올리면 안 되는 파일
- .streamlit/secrets.toml
- rental.db
- thumbnails 폴더
- quote_files 폴더
- Supabase Secret/Service Role Key가 적힌 파일

Streamlit Cloud Secrets 입력 형식
SUPABASE_URL = "https://프로젝트ID.supabase.co"
SUPABASE_SERVICE_KEY = "Supabase의 Secret key 또는 legacy service_role key"
APP_PASSWORD = "직원용 앱 접속 비밀번호"

배포 Entrypoint: app.py
