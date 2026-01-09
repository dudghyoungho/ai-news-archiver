const API_BASE_URL = "http://localhost:8000"; // 배포 시 실제 주소로 변경

document.addEventListener("DOMContentLoaded", async () => {
    // 1. HTML 요소 가져오기 (오타 방지)
    const loginSection = document.getElementById("login-section");
    const saveSection = document.getElementById("save-section");
    const statusDiv = document.getElementById("status");
    const urlDisplay = document.getElementById("current-url");
    const userInfo = document.getElementById("user-info");

    // 버튼 요소 가져오기
    const btnGoWeb = document.getElementById("btn-go-web");
    const btnOpenWeb = document.getElementById("btn-open-web");
    const btnSave = document.getElementById("btn-save");

    // 2. 초기 상태 확인 (쿠키 체크)
    try {
        const session = await getAuthCookies();
        console.log("Cookie Check:", JSON.stringify(session, null, 2)); // 디버깅용 로그

        const who = await fetch(`${API_BASE_URL}/api/whoami/`, { credentials: "include" });
        console.log("whoami status:", who.status, await who.text());

        if (session.sessionid) {
            // 세션 ID가 있으면 로그인된 상태
            showSaveSection();
        } else {
            showLoginSection();
        }
    } catch (e) {
        console.error("Cookie Error:", e);
        statusDiv.textContent = "쿠키 권한 오류: manifest.json을 확인하세요.";
    }

    // =========================================
    // 3. 이벤트 리스너 연결 (안전장치 추가)
    // =========================================

    // [로그인 버튼] 웹사이트 로그인 페이지 열기
    if (btnGoWeb) {
        btnGoWeb.addEventListener("click", () => {
            chrome.tabs.create({ url: `${API_BASE_URL}/accounts/login/` });
        });
    } else {
        console.error("❌ 'btn-go-web' 버튼을 찾을 수 없습니다.");
    }

    // [보관함 버튼] 메인 페이지 열기
    if (btnOpenWeb) {
        btnOpenWeb.addEventListener("click", () => {
            chrome.tabs.create({ url: API_BASE_URL });
        });
    }

    // [저장 버튼] 핵심 기능
    if (btnSave) {
        btnSave.addEventListener("click", async () => {
            statusDiv.textContent = "⏳ 저장 및 분석 중...";
            
            // 1. 현재 탭 URL 가져오기
            const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
            const currentTab = tabs[0];

            if (!currentTab || !currentTab.url.startsWith('http')) {
                statusDiv.textContent = "❌ 저장할 수 없는 페이지입니다.";
                return;
            }

            // 2. 최신 쿠키 다시 가져오기 (만료 체크)
            const cookies = await getAuthCookies();
            if (!cookies.sessionid) {
                statusDiv.textContent = "로그인이 풀렸습니다. 다시 로그인해주세요.";
                showLoginSection();
                return;
            }

            try {
                // 3. Django API 호출
                const response = await fetch(`${API_BASE_URL}/api/links/create/`, {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "Content-Type": "application/json",
                        // [중요] 세션 인증 시 CSRF 토큰 필수
                        "X-CSRFToken": cookies.csrftoken 
                    },
                    body: JSON.stringify({ url: currentTab.url })
                });

                if (response.ok) {
                    statusDiv.innerHTML = "✅ 저장 완료! <br>AI가 요약을 시작했습니다.";
                    setTimeout(() => window.close(), 2000); // 2초 뒤 닫기
                } else {
                    const err = await response.json();
                    // 에러 메시지가 객체인지 문자열인지 확인
                    const msg = err.detail || JSON.stringify(err);
                    statusDiv.textContent = "❌ 실패: " + msg;
                }
            } catch (error) {
                console.error("API Error:", error);
                statusDiv.textContent = "❌ 서버 연결 실패 (서버가 켜져있나요?)";
            }
        });
    }

    // =========================================
    // 4. 유틸리티 함수들
    // =========================================

    function showLoginSection() {
        if(loginSection) loginSection.classList.remove("hidden");
        if(saveSection) saveSection.classList.add("hidden");
    }

    function showSaveSection() {
        if(loginSection) loginSection.classList.add("hidden");
        if(saveSection) saveSection.classList.remove("hidden");
        
        // 현재 URL 표시
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (urlDisplay && tabs[0]) {
                urlDisplay.textContent = tabs[0].url;
            }
        });
    }

    // [디버깅용 getCookies 함수]
    function getCookie(name) {
      return new Promise((resolve) => {
        chrome.cookies.get({ url: API_BASE_URL, name }, (cookie) => {
          resolve(cookie ? cookie.value : null);
        });
      });
    }
    
    async function getAuthCookies() {
      const sessionid = await getCookie("sessionid");
      const csrftoken = await getCookie("csrftoken");
      return { sessionid, csrftoken };
    }
});