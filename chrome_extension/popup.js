const API_BASE_URL = "http://localhost:8000"; // Django 서버 주소

document.addEventListener("DOMContentLoaded", () => {
  const loginSection = document.getElementById("login-section");
  const saveSection = document.getElementById("save-section");
  const statusDiv = document.getElementById("status");
  const urlDisplay = document.getElementById("current-url");

  // 1. 저장된 토큰 확인 (로그인 유지)
  chrome.storage.local.get(["authToken"], (result) => {
    if (result.authToken) {
      showSaveSection();
    }
  });

  // 2. 로그인 처리
  document.getElementById("btn-login").addEventListener("click", async () => {
    const username = document.getElementById("username").value;
    const password = document.getElementById("password").value;

    try {
      // Django DRF Token Auth 엔드포인트가 필요함 (없으면 만들어야 함)
      // 임시로 /api-token-auth/ 라고 가정하거나, Basic Auth 사용 가능
      // 여기서는 편의상 Basic Auth 헤더를 생성하여 저장 테스트
      const token = btoa(`${username}:${password}`); // Basic Auth (ID:PW Base64 인코딩)

      // 로그인 검증 요청 (예: 프로필 조회)
      const response = await fetch(`${API_BASE_URL}/api/links/list/`, {
        headers: { Authorization: `Basic ${token}` },
      });

      if (response.ok) {
        chrome.storage.local.set({ authToken: token, authType: "Basic" });
        showSaveSection();
        statusDiv.textContent = "로그인 성공!";
      } else {
        statusDiv.textContent = "로그인 실패. 아이디/비번을 확인하세요.";
      }
    } catch (error) {
      statusDiv.textContent = "서버 오류: " + error.message;
    }
  });

  // 3. 현재 페이지 저장 요청
  document.getElementById("btn-save").addEventListener("click", async () => {
    chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
      const currentUrl = tabs[0].url;

      chrome.storage.local.get(["authToken", "authType"], async (data) => {
        if (!data.authToken) return;

        statusDiv.textContent = "요약 요청 중...";

        try {
          // 우리가 만든 API 호출
          const response = await fetch(`${API_BASE_URL}/api/links/create/`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `${data.authType} ${data.authToken}`,
            },
            body: JSON.stringify({ url: currentUrl }),
          });

          if (response.ok) {
            statusDiv.textContent = "✅ 저장 완료! AI가 분석을 시작합니다.";
            setTimeout(() => window.close(), 2000); // 2초 뒤 닫기
          } else {
            const err = await response.json();
            statusDiv.textContent =
              "실패: " + (err.detail || "알 수 없는 오류");
          }
        } catch (error) {
          statusDiv.textContent = "네트워크 오류 발생";
        }
      });
    });
  });

  // 4. 로그아웃
  document.getElementById("btn-logout").addEventListener("click", () => {
    chrome.storage.local.remove(["authToken", "authType"]);
    loginSection.classList.remove("hidden");
    saveSection.classList.add("hidden");
    statusDiv.textContent = "";
  });

  function showSaveSection() {
    loginSection.classList.add("hidden");
    saveSection.classList.remove("hidden");

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      urlDisplay.textContent = tabs[0].url;
    });
  }
});
