# Проверка PDF через Timeweb AI Gateway

Проверено 24 октября 2026, 02:04–02:05 MSK. Это диагностика, не изменение
production-модели или интеграции. Только искусственный одностраничный PDF
размером 699 байт: случайная контрольная строка и вымышленная дата.
Реальные сертификаты, коды и данные клиенток не отправлялись.

Маршрут: `https://api.timeweb.ai/v1`, модель `deepseek/deepseek-flash` — та же,
что настроена в production. Запросы без автоматических повторов, с обычной
проверкой TLS. Контрольный ответ также указал `deepseek/deepseek-flash`.

| Проверка | Результат |
| --- | --- |
| Обычный текст, контрольная строка и дата | HTTP 200, оба значения прочитаны правильно; 1,43 с |
| PDF inline в Chat Completions, вложенный OpenAI `file.file_data` | HTTP 400, ошибка формы file-блока |
| PDF inline, плоский DeepSeek `file_data` | HTTP 500, общая ошибка шлюза; конкретная причина не раскрыта |
| Files API без поля целевой модели | HTTP 400, требование указать модель; это не проверка поддержки PDF |
| Files API по документации Timeweb, `purpose=user_data`, `target_model_names=deepseek/deepseek-flash` | HTTP 400: `AIGateway doesn't support custom_openai for 'create_file'` |

**Вывод:** рабочий путь передачи исходного PDF текущему DeepSeek через Timeweb
не подтверждён. Документированный upload отклонён из-за неподдерживаемого
маршрута Files API. Ошибку 500 нельзя трактовать как доказательство конкретного
ограничения формата. Ни один upload не вернул идентификатор созданного файла.

Официальная документация DeepSeek перечисляет для Files API только JPEG,
PNG, GIF и WebP — PDF в списке нет. Наличие загрузки документов в пользовательском
веб-чате не устанавливает такой же контракт API. Timeweb документирует Files API
на примере TXT и другой модели, без обещания PDF для DeepSeek.

Для текущей реализации срок извлекается локально поддерживаемым парсером PDF;
в LLM передаются только проверенные поля поставщика и даты. Неопределённый срок
не подменяется семью днями. Исходный документ и реквизиты не входят в LLM-контекст.
Исторические документы автоматически не перечитываются.

Источники:

- [DeepSeek Files API](https://api-docs.deepseek.com/guides/files_api/)
- [Timeweb Files API](https://timeweb.cloud/docs/ai-gateway/files-api)
- [DeepSeek Responses compatibility](https://api-docs.deepseek.com/guides/responses_api/)

Закрытые диагностические результаты на Mac:
`.runtime/timeweb-pdf-probe-result.json` и `.runtime/timeweb-pdf-probe-result-v2.json`.
Не являются общедоступным API-контрактом и могут устареть после обновления провайдера.
