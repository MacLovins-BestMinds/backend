# ТЗ · Модуль авторизации Stage Zero

Версия: 1.0 (Октябрь 2026)  
Стек: FastAPI (бэкенд) + React Native / Expo (мобильное приложение)

---

## 1. Цели и сценарии

1. **Базовая авторизация (Логин + Пароль):**
   - Быстрая регистрация по никнейму и паролю.
   - Вход по никнейму и паролю с выдачей JWT-токена.
   - Хранение паролей в виде безопасного хэша (bcrypt / argon2).

2. **Авторизация через Google (Google Sign-In):**
   - Вход в один клик через нативный Google Sign-In в Expo / React Native.
   - Приложение получает `id_token` от Google SDK и отправляет на бэкенд.
   - Бэкенд верифицирует `id_token`, находит или автоматически регистрирует пользователя, привязывая его Google ID и email.

3. **Гостевой вход и обратная совместимость:**
   - Сохранение текущего легковесного эндпоинта `POST /api/game/auth` для мгновенного входа без регистрации.
   - Возможность привязать email/пароль или Google к существующему гостевому аккаунту с сохранением звания и истории раундов.

---

## 2. Схема базы данных (модель User)

Таблица `users` расширяется следующими полями:

| Поле | Тип | Описание |
| --- | --- | --- |
| `id` | `str` (PK) | Уникальный ID пользователя (`u_...`) |
| `nick` | `str` (Unique, Index) | Никнейм пользователя |
| `email` | `str?` (Unique, Index) | Email (обязателен для Google, опционален для local) |
| `password_hash` | `str?` | Хэш пароля (bcrypt). Nullable для входа через Google |
| `google_id` | `str?` (Unique, Index) | Идентификатор пользователя в Google (`sub`) |
| `avatar_url` | `str?` | Ссылка на аватар из Google профиля |
| `auth_provider` | `str` | `local` \| `google` \| `guest` |
| `created_at` | `datetime` | Дата и время регистрации (UTC) |

---

## 3. Контракты API

Базовый префикс маршрутов: `/api/auth`

### 3.1. Регистрация по логину и паролю
`POST /api/auth/register`

**Запрос:**
```json
{
  "nick": "alex_speaker",
  "password": "StrongPassword123"
}
```

**Ответ (201 Created):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni...",
  "token_type": "bearer",
  "user": {
    "user_id": "u_a1b2c3d4e5",
    "nick": "alex_speaker",
    "email": null,
    "auth_provider": "local",
    "rank": {
      "title": "Новичок",
      "trend": "flat"
    }
  }
}
```

**Ошибки:**
- `400 Bad Request` — никнейм уже занят; пароль короче 6 символов.

---

### 3.2. Вход по логину и паролю
`POST /api/auth/login`

**Запрос:**
```json
{
  "nick": "alex_speaker",
  "password": "StrongPassword123"
}
```

**Ответ (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni...",
  "token_type": "bearer",
  "user": {
    "user_id": "u_a1b2c3d4e5",
    "nick": "alex_speaker",
    "email": null,
    "auth_provider": "local",
    "rank": {
      "title": "Питчер",
      "trend": "up"
    }
  }
}
```

**Ошибки:**
- `401 Unauthorized` — неверный логин или пароль.

---

### 3.3. Вход через Google
`POST /api/auth/google`

Клиент (телефон) использует Google Sign-In SDK (`@react-native-google-signin/google-signin` или `expo-auth-session`), получает от Google криптографический `id_token` и передает его на бэкенд.

**Запрос:**
```json
{
  "id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6...",
  "guest_user_id": "u_optional_guest_id" 
}
```
*(поле `guest_user_id` опционально: если передано, прогресс гостя привязывается к аккаунту Google).*

**Логика бэкенда:**
1. Верификация токена через Google OAuth Public Keys (библиотека `google-auth-library` / `google-auth`).
2. Извлечение `google_id`, `email`, `name`, `picture`.
3. Поиск пользователя по `google_id` или `email`:
   - Если найден — вход.
   - Если не найден — создание нового пользователя (`nick` берется из `name` или префикса email).
4. Выпуск внутреннего Stage Zero JWT токена.

**Ответ (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni...",
  "token_type": "bearer",
  "user": {
    "user_id": "u_g_9876543210",
    "nick": "Alexey",
    "email": "alex@gmail.com",
    "avatar_url": "https://lh3.googleusercontent.com/...",
    "auth_provider": "google",
    "rank": {
      "title": "Питчер",
      "trend": "up"
    }
  }
}
```

---

### 3.4. Получение текущего профиля (Me)
`GET /api/auth/me`  
Заголовок: `Authorization: Bearer <access_token>`

**Ответ (200 OK):**
```json
{
  "user_id": "u_a1b2c3d4e5",
  "nick": "alex_speaker",
  "email": "alex@gmail.com",
  "avatar_url": null,
  "auth_provider": "local",
  "rank": {
    "title": "Питчер",
    "trend": "up"
  }
}
```

---

## 4. Защита игровых эндпоинтов

Для всех эндпоинтов `/api/game/*` поддерживается:
1. Авторизация по заголовку `Authorization: Bearer <token>` (пользователь извлекается из токена).
2. Обратная совместимость: если передан `user_id` в теле или query, запрос принимается как раньше (гостевой режим).

---

## 5. Переменные окружения (.env)

```env
# JWT
JWT_SECRET_KEY=super_secret_stage_zero_key_change_in_production
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_DAYS=30

# Google OAuth
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
```

---

## 6. План внедрения

1. **Бэкенд (Егор):**
   - Добавить библиотеки `passlib[bcrypt]`, `pyjwt`, `google-auth`.
   - Обновить модель `User` в БД.
   - Создать модуль `app/auth/` (хэширование, JWT утилиты, валидация Google токена, роутер `/api/auth`).
   - Добавить зависимость `get_current_user` для защиты эндпоинтов с сохранением обратной совместимости.
   - Добавить автотесты на регистрацию, логин и Google auth.

2. **Мобильное приложение (Артём):**
   - Экран входа с двумя кнопками: «Войти через Google» и «Логин / Пароль», плюс ссылка «Продолжить как гость».
   - Сохранение `access_token` в `SecureStore` / `AsyncStorage`.
   - Подстановка `Authorization: Bearer <token>` в заголовок HTTP-клиента.
