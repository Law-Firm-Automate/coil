"""Client-facing strings in English and formal (usted) Spanish.

Nothing here is registered globally. Routes import `t` and `lang_for` and pass them to their templates.
The public invoice page and the engagement sign pages are rendered by other modules, so `portal.py` exposes
`t` and `lang_for` to templates through a blueprint-level context processor (explicit render kwargs still win).

Usage: t("portal.home.title", lang) or t("inv.title", lang, number="INV-1001", firm="Demo Law").
Missing Spanish keys fall back to English; unknown keys return the key itself so a typo is visible, not a crash.
"""

LANGS = ("en", "es")

T = {
    "en": {
        # ---- portal: login, expired, nav ----
        "portal.title": "Client portal",
        "portal.login.intro": "Enter the email address we have on file and we will send you a sign-in link. No password needed.",
        "portal.login.email": "Email",
        "portal.login.button": "Send me a sign-in link",
        "portal.login.neutral": "If we have that email on file, we sent you a sign-in link. It works for 30 minutes.",
        "portal.expired.title": "That link has expired",
        "portal.expired.body": "Sign-in links work once and expire after 30 minutes.",
        "portal.expired.request": "Request a new one",
        "portal.nav.home": "Home",
        "portal.nav.messages": "Messages",
        "portal.nav.logout": "Log out",
        "portal.logged_out": "You are signed out.",
        # ---- portal: home ----
        "portal.home.title": "Your portal",
        "portal.home.welcome": "Welcome, {name}",
        "portal.home.intro": "Here is where things stand with your matters at {firm}.",
        "portal.home.letters": "Engagement letters awaiting your signature",
        "portal.home.docs_to_sign": "Documents awaiting your signature",
        "portal.home.sent_on": "Sent {date}",
        "portal.home.review_sign": "Review and sign",
        "portal.home.messages": "Secure messages",
        "portal.home.unread": "{n} unread",
        "portal.home.no_unread": "No unread messages.",
        "portal.home.open_messages": "Open messages",
        "portal.home.invoices": "Invoices due",
        "portal.home.invoice": "Invoice",
        "portal.home.matter": "Matter",
        "portal.home.due": "Due",
        "portal.home.balance": "Balance",
        "portal.home.past_due": "past due",
        "portal.home.view_pay": "View and pay",
        "portal.home.nothing_due": "Nothing is due right now.",
        "portal.home.matters": "Your matters",
        "portal.home.number": "Number",
        "portal.home.status": "Status",
        "portal.home.attorney": "Attorney",
        "portal.home.no_matters": "No open matters.",
        "portal.status.open": "open",
        "portal.status.pending": "pending",
        "portal.status.closed": "closed",
        "portal.home.docs": "Documents shared with you",
        "portal.home.uploaded_by_you": "uploaded by you",
        "portal.home.download": "Download",
        "portal.home.no_docs": "No documents have been shared yet.",
        "portal.home.send_doc": "Send us a document",
        "portal.home.file": "File",
        "portal.home.upload": "Upload",
        "portal.home.no_matters_upload": "Once you have a matter with us you can upload documents here.",
        "portal.home.trust": "Trust balance",
        "portal.home.trust_body": "{amount} of your money is held in our client trust account. It stays yours until it is "
                                  "applied to an invoice for work already done, and anything unused is returned to you.",
        "portal.upload.pick_matter": "Pick one of your matters.",
        "portal.upload.choose_file": "Choose a file to upload.",
        "portal.upload.done": "Uploaded {name}. We will take a look.",
        # ---- portal: messages ----
        "portal.msgs.title": "Messages",
        "portal.msgs.intro": "Messages sent here stay inside the portal. We are notified when you write, and you will get "
                             "an email when we reply.",
        "portal.msgs.all": "All",
        "portal.msgs.general": "General",
        "portal.msgs.you": "You",
        "portal.msgs.firm": "{firm}",
        "portal.msgs.read": "read",
        "portal.msgs.empty": "No messages yet. Write the first one below.",
        "portal.msgs.new": "New message",
        "portal.msgs.regarding": "Regarding",
        "portal.msgs.placeholder": "Type your message",
        "portal.msgs.send": "Send",
        "portal.msgs.sent": "Your message was sent.",
        "portal.msgs.empty_body": "Type a message first.",
        # ---- sign pages (documents and engagement letters) ----
        "sign.doc.default_title": "Document for signature",
        "sign.doc.from": "{firm} has asked you to sign this document.",
        "sign.doc.open": "Open the document",
        "sign.doc.download": "Download the document",
        "sign.doc.preview_note": "If the preview does not load, use the download link.",
        "sign.doc.section": "Sign this document",
        "sign.letter.section": "Sign this letter",
        "sign.name": "Your full name (this is your signature)",
        "sign.email": "Email",
        "sign.agree_doc": "I have read this document and agree to sign it electronically.",
        "sign.agree_letter": "I have read this letter and agree to its terms. I agree to sign it electronically.",
        "sign.button": "Sign",
        "sign.decline_summary": "I do not wish to sign",
        "sign.reason": "Reason (optional)",
        "sign.decline": "Decline",
        "sign.decline_confirm_doc": "Decline to sign this document?",
        "sign.decline_confirm_letter": "Decline this engagement letter?",
        "sign.record_note": "Your name, IP address, browser, and the time of signing are recorded and attached to the signed copy.",
        "sign.err_name": "Type your full name and tick the box to confirm you agree.",
        "sign.err_agree_doc": "Please tick the box to confirm you have read the document and agree to sign it.",
        "sign.err_agree_letter": "Please tick the box to confirm you have read the letter and agree to its terms.",
        "sign.done.title": "Signed",
        "sign.done.thanks": "Thank you, {name}",
        "sign.done.recorded_letter": "Your signature was recorded on {when} UTC. A signed copy has been emailed to {email} and to {firm}.",
        "sign.done.recorded_doc": "Your signature was recorded on {when} UTC. The signature certificate and the document have "
                                  "been emailed to {email} and to {firm}.",
        "sign.done.you": "you",
        "sign.done.download_pdf": "Download the signed PDF",
        "sign.done.download_cert": "Download the signature certificate",
        "sign.done.doc_hash": "Document hash",
        "sign.done.sig_hash": "Signature hash",
        "sign.contact_at": "{firm} at {phone}",
        "sign.status.signed_letter": "This letter was signed by {name} on {when} UTC.",
        "sign.status.signed_doc": "This document was signed by {name} on {when} UTC.",
        "sign.status.declined_letter": "This letter was declined. If that was a mistake, contact {contact}.",
        "sign.status.declined_doc": "You declined to sign this document. If that was a mistake, contact {contact}.",
        "sign.status.void_letter": "This letter is no longer available. Contact {contact} if you were expecting to sign it.",
        "sign.status.void_doc": "This signature request is no longer available. Contact {contact} if you were expecting to sign it.",
        "sign.status.not_sent_letter": "This letter has not been sent yet.",
        "sign.status.not_sent_doc": "This document has not been sent for signature yet.",
        # ---- public invoice page ----
        "inv.title": "Invoice {number} from {firm}",
        "inv.heading": "Invoice {number}",
        "inv.void": "Void",
        "inv.paid_stamp": "PAID",
        "inv.balance_due": "Balance due",
        "inv.bill_to": "Bill to",
        "inv.matter": "Matter",
        "inv.issued": "Issued",
        "inv.due": "Due",
        "inv.on_receipt": "On receipt",
        "inv.from": "From",
        "inv.date": "Date",
        "inv.description": "Description",
        "inv.qty": "Qty",
        "inv.rate": "Rate",
        "inv.amount": "Amount",
        "inv.subtotal": "Subtotal",
        "inv.tax": "Tax",
        "inv.paid": "Paid",
        "inv.pay_this": "Pay this invoice",
        "inv.pay_ach": "Pay by bank transfer (ACH), no fee",
        "inv.pay_card": "Pay by card",
        "inv.surcharge": "A {pct}% card surcharge applies.",
        "inv.trust_note": "You have {amount} on deposit in our trust account. If you would rather we apply those funds to this "
                          "invoice, reply to the email or call {phone} and we will take care of it.",
        "inv.our_office": "our office",
        "inv.check": "Prefer to mail a check? Make it payable to {firm}{send_to}.",
        "inv.send_to": " and send it to {address}",
        "inv.payments_received": "Payments received",
        "inv.method": "Method",
        "inv.download_pdf": "Download PDF",
        # ---- client emails ----
        "email.hello": "Hello {name},",
        "email.fallback_link": "If the button does not work, open this link: {url}",
        "email.portal_link.subject": "Your sign-in link for {firm}",
        "email.portal_link.body": "Use this link to sign in to your client portal at {firm}. It works once and expires in "
                                  "{minutes} minutes.",
        "email.portal_link.ignore": "If you did not ask for this, you can ignore this email.",
        "email.portal_link.text": "Sign in to your client portal at {firm} with this link (one use, {minutes} minutes):\n{url}\n\n"
                                  "If you did not ask for this, ignore this email.",
        "email.new_message.subject": "You have a new secure message from {firm}",
        "email.new_message.body": "{firm} sent you a secure message about {about}. Sign in to your client portal to read it "
                                  "and reply. For your privacy the message itself is not included in this email.",
        "email.new_message.about_account": "your account",
        "email.new_message.button": "Open the portal",
        "email.new_message.text": "{firm} sent you a secure message. Sign in to your client portal to read it: {url}",
        "email.sig_request.subject": "Please sign: {title}",
        "email.sig_request.body": "{firm} has sent you {title} to review and sign electronically. Use the button below.",
        "email.sig_request.message_from": "Message from {firm}:",
        "email.sig_request.button": "Review and sign",
        "email.sig_request.text": "Please review and sign {title}: {url}",
        "email.sig_reminder.subject": "Reminder: {title}",
        "email.sig_reminder.body": "This is a reminder that {title} from {firm} is waiting for your signature.",
        "email.signed.subject": "Signed: {title}",
        "email.signed.body": "Thank you, {name}. Your signature certificate and a copy of {title} are attached for your records.",
        "email.signed.button": "Download the certificate",
        "email.signed.text": "Your signature certificate and a copy of {title} are attached.",
    },
    "es": {
        # ---- portal: login, expired, nav ----
        "portal.title": "Portal del cliente",
        "portal.login.intro": "Escriba el correo electrónico que tenemos registrado y le enviaremos un enlace para iniciar "
                              "sesión. No necesita contraseña.",
        "portal.login.email": "Correo electrónico",
        "portal.login.button": "Enviarme un enlace de acceso",
        "portal.login.neutral": "Si tenemos ese correo registrado, le hemos enviado un enlace de acceso. Es válido durante 30 minutos.",
        "portal.expired.title": "Ese enlace ha caducado",
        "portal.expired.body": "Los enlaces de acceso funcionan una sola vez y caducan a los 30 minutos.",
        "portal.expired.request": "Solicite uno nuevo",
        "portal.nav.home": "Inicio",
        "portal.nav.messages": "Mensajes",
        "portal.nav.logout": "Cerrar sesión",
        "portal.logged_out": "Ha cerrado la sesión.",
        # ---- portal: home ----
        "portal.home.title": "Su portal",
        "portal.home.welcome": "Le damos la bienvenida, {name}",
        "portal.home.intro": "Aquí puede consultar el estado de sus asuntos con {firm}.",
        "portal.home.letters": "Cartas de contratación pendientes de su firma",
        "portal.home.docs_to_sign": "Documentos pendientes de su firma",
        "portal.home.sent_on": "Fecha de envío: {date}",
        "portal.home.review_sign": "Revisar y firmar",
        "portal.home.messages": "Mensajes seguros",
        "portal.home.unread": "{n} sin leer",
        "portal.home.no_unread": "No tiene mensajes sin leer.",
        "portal.home.open_messages": "Ver mensajes",
        "portal.home.invoices": "Facturas pendientes de pago",
        "portal.home.invoice": "Factura",
        "portal.home.matter": "Asunto",
        "portal.home.due": "Vencimiento",
        "portal.home.balance": "Saldo",
        "portal.home.past_due": "vencida",
        "portal.home.view_pay": "Ver y pagar",
        "portal.home.nothing_due": "No tiene ningún pago pendiente en este momento.",
        "portal.home.matters": "Sus asuntos",
        "portal.home.number": "Número",
        "portal.home.status": "Estado",
        "portal.home.attorney": "Abogado",
        "portal.home.no_matters": "No tiene asuntos abiertos.",
        "portal.status.open": "abierto",
        "portal.status.pending": "pendiente",
        "portal.status.closed": "cerrado",
        "portal.home.docs": "Documentos compartidos con usted",
        "portal.home.uploaded_by_you": "subido por usted",
        "portal.home.download": "Descargar",
        "portal.home.no_docs": "Todavía no se ha compartido ningún documento.",
        "portal.home.send_doc": "Envíenos un documento",
        "portal.home.file": "Archivo",
        "portal.home.upload": "Subir",
        "portal.home.no_matters_upload": "Cuando tenga un asunto con nosotros podrá subir documentos aquí.",
        "portal.home.trust": "Saldo en cuenta fiduciaria",
        "portal.home.trust_body": "{amount} de su dinero se encuentra depositado en nuestra cuenta fiduciaria de clientes. "
                                  "Sigue siendo suyo hasta que se aplique a una factura por trabajo ya realizado, y cualquier "
                                  "saldo no utilizado se le devolverá.",
        "portal.upload.pick_matter": "Seleccione uno de sus asuntos.",
        "portal.upload.choose_file": "Seleccione un archivo para subir.",
        "portal.upload.done": "Se subió {name}. Lo revisaremos.",
        # ---- portal: messages ----
        "portal.msgs.title": "Mensajes",
        "portal.msgs.intro": "Los mensajes enviados aquí permanecen dentro del portal. Recibimos un aviso cuando usted escribe "
                             "y usted recibirá un correo cuando le respondamos.",
        "portal.msgs.all": "Todos",
        "portal.msgs.general": "General",
        "portal.msgs.you": "Usted",
        "portal.msgs.firm": "{firm}",
        "portal.msgs.read": "leído",
        "portal.msgs.empty": "Todavía no hay mensajes. Escriba el primero a continuación.",
        "portal.msgs.new": "Nuevo mensaje",
        "portal.msgs.regarding": "Relacionado con",
        "portal.msgs.placeholder": "Escriba su mensaje",
        "portal.msgs.send": "Enviar",
        "portal.msgs.sent": "Su mensaje ha sido enviado.",
        "portal.msgs.empty_body": "Escriba un mensaje primero.",
        # ---- sign pages ----
        "sign.doc.default_title": "Documento para firmar",
        "sign.doc.from": "{firm} le solicita que firme este documento.",
        "sign.doc.open": "Abrir el documento",
        "sign.doc.download": "Descargar el documento",
        "sign.doc.preview_note": "Si la vista previa no se carga, use el enlace de descarga.",
        "sign.doc.section": "Firmar este documento",
        "sign.letter.section": "Firmar esta carta",
        "sign.name": "Su nombre completo (esta es su firma)",
        "sign.email": "Correo electrónico",
        "sign.agree_doc": "He leído este documento y acepto firmarlo electrónicamente.",
        "sign.agree_letter": "He leído esta carta y acepto sus términos. Acepto firmarla electrónicamente.",
        "sign.button": "Firmar",
        "sign.decline_summary": "No deseo firmar",
        "sign.reason": "Motivo (opcional)",
        "sign.decline": "Rechazar",
        "sign.decline_confirm_doc": "¿Desea rechazar la firma de este documento?",
        "sign.decline_confirm_letter": "¿Desea rechazar esta carta de contratación?",
        "sign.record_note": "Su nombre, dirección IP, navegador y la hora de la firma quedan registrados y se adjuntan a la "
                            "copia firmada.",
        "sign.err_name": "Escriba su nombre completo y marque la casilla para confirmar que está de acuerdo.",
        "sign.err_agree_doc": "Marque la casilla para confirmar que ha leído el documento y acepta firmarlo.",
        "sign.err_agree_letter": "Marque la casilla para confirmar que ha leído la carta y acepta sus términos.",
        "sign.done.title": "Firmado",
        "sign.done.thanks": "Gracias, {name}",
        "sign.done.recorded_letter": "Su firma quedó registrada el {when} UTC. Se ha enviado una copia firmada por correo "
                                     "electrónico a {email} y a {firm}.",
        "sign.done.recorded_doc": "Su firma quedó registrada el {when} UTC. El certificado de firma y el documento se han "
                                  "enviado por correo electrónico a {email} y a {firm}.",
        "sign.done.you": "usted",
        "sign.done.download_pdf": "Descargar el PDF firmado",
        "sign.done.download_cert": "Descargar el certificado de firma",
        "sign.done.doc_hash": "Hash del documento",
        "sign.done.sig_hash": "Hash de la firma",
        "sign.contact_at": "{firm} al {phone}",
        "sign.status.signed_letter": "Esta carta fue firmada por {name} el {when} UTC.",
        "sign.status.signed_doc": "Este documento fue firmado por {name} el {when} UTC.",
        "sign.status.declined_letter": "Esta carta fue rechazada. Si fue un error, comuníquese con {contact}.",
        "sign.status.declined_doc": "Usted rechazó firmar este documento. Si fue un error, comuníquese con {contact}.",
        "sign.status.void_letter": "Esta carta ya no está disponible. Comuníquese con {contact} si esperaba firmarla.",
        "sign.status.void_doc": "Esta solicitud de firma ya no está disponible. Comuníquese con {contact} si esperaba firmar "
                                "el documento.",
        "sign.status.not_sent_letter": "Esta carta todavía no ha sido enviada.",
        "sign.status.not_sent_doc": "Este documento todavía no ha sido enviado para su firma.",
        # ---- public invoice page ----
        "inv.title": "Factura {number} de {firm}",
        "inv.heading": "Factura {number}",
        "inv.void": "Anulada",
        "inv.paid_stamp": "PAGADA",
        "inv.balance_due": "Saldo pendiente",
        "inv.bill_to": "Facturar a",
        "inv.matter": "Asunto",
        "inv.issued": "Fecha de emisión",
        "inv.due": "Vencimiento",
        "inv.on_receipt": "Al recibirla",
        "inv.from": "De",
        "inv.date": "Fecha",
        "inv.description": "Descripción",
        "inv.qty": "Cant.",
        "inv.rate": "Tarifa",
        "inv.amount": "Importe",
        "inv.subtotal": "Subtotal",
        "inv.tax": "Impuestos",
        "inv.paid": "Pagado",
        "inv.pay_this": "Pagar esta factura",
        "inv.pay_ach": "Pagar por transferencia bancaria (ACH), sin comisión",
        "inv.pay_card": "Pagar con tarjeta",
        "inv.surcharge": "Se aplica un recargo del {pct}% por pago con tarjeta.",
        "inv.trust_note": "Usted tiene {amount} depositados en nuestra cuenta fiduciaria. Si prefiere que apliquemos esos "
                          "fondos a esta factura, responda al correo electrónico o llame al {phone} y nos encargaremos.",
        "inv.our_office": "nuestra oficina",
        "inv.check": "¿Prefiere enviar un cheque por correo? Emítalo a nombre de {firm}{send_to}.",
        "inv.send_to": " y envíelo a {address}",
        "inv.payments_received": "Pagos recibidos",
        "inv.method": "Método",
        "inv.download_pdf": "Descargar PDF",
        # ---- client emails ----
        "email.hello": "Estimado(a) {name}:",
        "email.fallback_link": "Si el botón no funciona, abra este enlace: {url}",
        "email.portal_link.subject": "Su enlace de acceso a {firm}",
        "email.portal_link.body": "Use este enlace para acceder a su portal de cliente de {firm}. Funciona una sola vez y "
                                  "caduca en {minutes} minutos.",
        "email.portal_link.ignore": "Si usted no solicitó este enlace, puede ignorar este correo.",
        "email.portal_link.text": "Acceda a su portal de cliente de {firm} con este enlace (un solo uso, {minutes} minutos):\n"
                                  "{url}\n\nSi usted no solicitó este enlace, ignore este correo.",
        "email.new_message.subject": "Tiene un nuevo mensaje seguro de {firm}",
        "email.new_message.body": "{firm} le ha enviado un mensaje seguro sobre {about}. Inicie sesión en su portal de "
                                  "cliente para leerlo y responder. Por su privacidad, el contenido del mensaje no se incluye "
                                  "en este correo.",
        "email.new_message.about_account": "su cuenta",
        "email.new_message.button": "Abrir el portal",
        "email.new_message.text": "{firm} le ha enviado un mensaje seguro. Inicie sesión en su portal de cliente para leerlo: {url}",
        "email.sig_request.subject": "Firma requerida: {title}",
        "email.sig_request.body": "{firm} le ha enviado {title} para que lo revise y lo firme electrónicamente. Use el botón "
                                  "que aparece a continuación.",
        "email.sig_request.message_from": "Mensaje de {firm}:",
        "email.sig_request.button": "Revisar y firmar",
        "email.sig_request.text": "Revise y firme {title} aquí: {url}",
        "email.sig_reminder.subject": "Recordatorio: {title}",
        "email.sig_reminder.body": "Le recordamos que {title}, enviado por {firm}, está pendiente de su firma.",
        "email.signed.subject": "Firmado: {title}",
        "email.signed.body": "Gracias, {name}. Se adjuntan el certificado de firma y una copia de {title} para su archivo.",
        "email.signed.button": "Descargar el certificado",
        "email.signed.text": "Se adjuntan el certificado de firma y una copia de {title}.",
    },
}


class _Safe(dict):
    """format_map helper: an unknown placeholder renders as-is instead of raising."""

    def __missing__(self, key):
        return "{" + key + "}"


def t(key, lang="en", **kw):
    """Translate `key` into `lang`, filling {placeholders} from kw. Falls back to English, then to the key."""
    table = T.get(lang) or T["en"]
    s = table.get(key)
    if s is None:
        s = T["en"].get(key, key)
    if kw:
        try:
            return s.format_map(_Safe(kw))
        except (ValueError, IndexError):
            return s
    return s


def lang_for(contact=None):
    """The language to speak to this contact: their own setting, else the firm default, else English."""
    lang = (getattr(contact, "language", "") or "").strip().lower() if contact is not None else ""
    if not lang:
        from .models import Firm
        lang = (Firm.get().default_language or "").strip().lower()
    return lang if lang in T else "en"
