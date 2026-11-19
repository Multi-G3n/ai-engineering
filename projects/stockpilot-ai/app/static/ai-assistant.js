
/* ==========================================
   STOCKPILOT AI ASSISTANT
   ========================================== */

document.addEventListener("DOMContentLoaded", () => {

    const form = document.getElementById("aiChatForm");

    if (!form) {
        return;
    }

    const questionInput = document.getElementById("aiQuestion");
    const messages = document.getElementById("aiMessages");
    const sendButton = document.getElementById("aiSendButton");
    const promptButtons = document.querySelectorAll(".ai-prompt");

    const csrfToken = JSON.parse(
        document.getElementById("aiCsrfToken").textContent
    );

    let busy = false;


    /* ADD CHAT MESSAGE */

    function addMessage(role, content) {

        const wrapper = document.createElement("div");

        wrapper.className = `ai-message ${role}`;

        if (role === "assistant") {

            const avatar = document.createElement("div");

            avatar.className = "ai-message-avatar";

            avatar.textContent = "✦";

            wrapper.appendChild(avatar);
        }

        const bubble = document.createElement("div");

        bubble.className = "ai-bubble";

        // Display AI output as text, not executable HTML.
        bubble.textContent = content;

        wrapper.appendChild(bubble);

        messages.appendChild(wrapper);

        messages.scrollTop = messages.scrollHeight;

        return wrapper;
    }


    /* LOADING STATE */

    function setBusy(value) {

        busy = value;

        sendButton.disabled = value;

        questionInput.disabled = value;

        promptButtons.forEach((button) => {
            button.disabled = value;
        });

        sendButton.textContent = value
            ? "Analyzing..."
            : "Ask AI →";
    }


    /* SEND QUESTION */

    async function askAI(question) {

        if (busy) {
            return;
        }

        question = question.trim();

        if (question.length < 3 || question.length > 500) {

            addMessage(
                "assistant",
                "Please enter a question between 3 and 500 characters."
            );

            return;
        }

        addMessage("user", question);

        questionInput.value = "";

        setBusy(true);

        const loadingMessage = addMessage(
            "assistant",
            "Analyzing your inventory..."
        );

        try {

            const response = await fetch("/ai/chat", {

                method: "POST",

                credentials: "same-origin",

                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrfToken
                },

                body: JSON.stringify({
                    question: question
                })

            });

            if (
                response.redirected &&
                new URL(response.url).pathname === "/login"
            ) {
                window.location.assign("/login");
                return;
            }

            const data = await response.json();

            if (!response.ok) {

                throw new Error(
                    typeof data.detail === "string"
                        ? data.detail
                        : "Unable to complete the request."
                );
            }

            loadingMessage.querySelector(
                ".ai-bubble"
            ).textContent = data.answer ||
                "No answer was returned.";

        } catch (error) {

            loadingMessage.querySelector(
                ".ai-bubble"
            ).textContent =
                error.message ||
                "Something went wrong. Please try again.";

        } finally {

            setBusy(false);

            messages.scrollTop = messages.scrollHeight;
        }
    }


    /* FORM SUBMISSION */

    form.addEventListener("submit", (event) => {

        event.preventDefault();

        askAI(questionInput.value);
    });


    /* SUGGESTED QUESTIONS */

    promptButtons.forEach((button) => {

        button.addEventListener("click", () => {

            askAI(button.dataset.question);

        });

    });

});

