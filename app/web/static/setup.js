/* Первый запуск: создание администратора.

   Пароль уходит только в базу, в виде хеша. Никаких .env и переменных
   окружения на этом пути нет — именно поэтому эта форма и существует. */

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('setup-form');
  const errorBox = document.getElementById('setup-error');
  const submit = document.getElementById('setup-submit');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.hidden = true;

    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    const confirm = document.getElementById('password2').value;

    // Проверяем здесь же: сервер всё равно проверит, но лишний запрос
    // ради опечатки ни к чему.
    if (password !== confirm) {
      errorBox.textContent = 'Пароли не совпадают';
      errorBox.hidden = false;
      return;
    }
    if (password.length < 8) {
      errorBox.textContent = 'Пароль короче 8 символов';
      errorBox.hidden = false;
      return;
    }

    submit.disabled = true;
    submit.textContent = 'Создаю…';

    try {
      const response = await fetch('/api/web/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username, password }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || 'Не удалось создать администратора');
      }
      window.location.href = '/app';
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      submit.disabled = false;
      submit.textContent = 'Создать и войти';
    }
  });
});
