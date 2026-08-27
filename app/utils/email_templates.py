"""Plantillas HTML para correos electrónicos."""


def credentials_body(app_name: str, email_to: str, password: str, login_url: str) -> str:
    return f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>Bienvenido a {app_name}</h2>
    <p>Tu cuenta ha sido creada exitosamente. Estas son tus credenciales de acceso:</p>
    <p><strong>Correo:</strong> {email_to}</p>
    <p><strong>Contraseña:</strong> {password}</p>
    <p>Puedes acceder en el siguiente enlace:</p>
    <p><a href="{login_url}">{login_url}</a></p>
</body>
</html>"""


def issued_body(
    app_name: str,
    student_name: str,
    verify_link: str,
    certificate_type_name: str | None = None,
) -> str:
    cert_line = (
        f'<p><strong>Certificado:</strong> {certificate_type_name}</p>'
        if certificate_type_name
        else ""
    )
    return f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>Certificado Emitido</h2>
    <p>Hola <strong>{student_name}</strong>,</p>
    <p>Te informamos que tu certificado ha sido emitido exitosamente.</p>
    {cert_line}
    <p>Puedes verificar y descargar tu certificado en el siguiente enlace:</p>
    <p><a href="{verify_link}">{verify_link}</a></p>
    <p>Si tienes alguna duda, por favor contacta al administrador.</p>
</body>
</html>"""


def contact_body(name: str, email: str, program: str, message: str) -> str:
    return f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>Nuevo contacto desde la web</h2>
    <table style="border-collapse: collapse; width: 100%;">
        <tr><td style="padding: 8px; font-weight: bold;">Nombre:</td><td style="padding: 8px;">{name}</td></tr>
        <tr><td style="padding: 8px; font-weight: bold;">Email:</td><td style="padding: 8px;">{email}</td></tr>
        <tr><td style="padding: 8px; font-weight: bold;">Programa de interés:</td><td style="padding: 8px;">{program}</td></tr>
    </table>
    <h3>Mensaje:</h3>
    <p style="background: #f5f5f5; padding: 16px; border-radius: 8px;">{message}</p>
</body>
</html>"""


def expired_body(
    app_name: str,
    student_name: str,
    certificate_type_name: str | None = None,
) -> str:
    cert_line = (
        f'<p><strong>Certificado:</strong> {certificate_type_name}</p>'
        if certificate_type_name
        else ""
    )
    return f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>Certificado Expirado</h2>
    <p>Hola <strong>{student_name}</strong>,</p>
    <p>Te informamos que tu certificado ha expirado.</p>
    {cert_line}
    <p>Si deseas obtener un nuevo certificado, por favor contacta al administrador.</p>
</body>
</html>"""


def backup_body(
    app_name: str,
    *,
    filename: str,
    size_bytes: int,
    uploaded: bool,
    extra: str | None = None,
) -> str:
    size_mb = size_bytes / (1024 * 1024)
    destino = "MinIO externo (backup)" if uploaded else "adjunto a este correo"
    status_html = "OK" if uploaded else "FALLIDO"
    color = "#198754" if uploaded else "#dc3545"
    extra_html = f"<p>{extra}</p>" if extra else ""
    return f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>Backup de base de datos — {app_name}</h2>
    <p style="color:{color}; font-weight: bold;">Estado: {status_html}</p>
    <p><strong>Archivo:</strong> {filename}</p>
    <p><strong>Tamaño:</strong> {size_mb:.2f} MB</p>
    <p><strong>Destino:</strong> {destino}</p>
    {extra_html}
</body>
</html>"""
