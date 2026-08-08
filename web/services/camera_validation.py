"""Pre-save ONVIF and RTSP validation for door camera settings."""

from urllib.parse import quote, urlparse, urlunparse


def _result(ok, status=200, error=None):
    return {'ok': ok, 'status': status, 'error': error}


def _select_stream_url(urls, camera_path, camera_ip, camera_port):
    """Choose the configured profile without logging or returning secrets."""
    path = camera_path or '/stream2'
    if path.startswith('/onvif/'):
        profile = path.rsplit('/', 1)[-1].lower()
        aliases = {
            'main': ('main', 'primary'),
            'sub': ('sub', 'minor', 'secondary'),
        }.get(profile, (profile,))
        for entry in urls:
            label = entry.get('profile', '').lower()
            if any(alias in label for alias in aliases):
                return entry.get('url', '')
        return urls[0].get('url', '') if urls else ''

    for entry in urls:
        url = entry.get('url', '')
        if urlparse(url).path == path:
            return url
    return f'rtsp://{camera_ip}:{camera_port}{path}'


def _inject_credentials(url, username, password):
    parsed = urlparse(url)
    userinfo = quote(username or '', safe='')
    if password:
        userinfo += ':' + quote(password, safe='')
    host = parsed.hostname or ''
    if parsed.port:
        host += f':{parsed.port}'
    netloc = f'{userinfo}@{host}' if userinfo else host
    return urlunparse(parsed._replace(netloc=netloc))


def validate_camera(camera, username, password, camera_path, camera_port,
                    fetch_rtsp=None, probe_stream=None):
    """Validate ONVIF credentials and one real RTSP video stream.

    Dependencies are injectable so tests never contact a camera or import the
    computer-vision stack. The result contains only safe user-facing text.
    """
    if fetch_rtsp is None:
        from ..discovery import fetch_camera_rtsp
        fetch_rtsp = fetch_camera_rtsp
    if probe_stream is None:
        from video_decode import probe_stream_diagnostic
        probe_stream = probe_stream_diagnostic

    urls, onvif_failure = fetch_rtsp(
        camera['ip'], username=username, password=password,
        xaddr=camera.get('xaddr', ''), diagnostics=True)

    if onvif_failure == 'auth':
        return _result(
            False, 422,
            'Camera rejected the username or password. Check the camera '
            'credentials and try again.')
    if onvif_failure == 'unreachable':
        return _result(
            False, 503,
            'Camera is unreachable at its ONVIF endpoint. Check its power, '
            'network connection, address, and port, then try again.')
    if onvif_failure or not urls:
        return _result(
            False, 422,
            'ONVIF login succeeded, but the camera did not provide a usable '
            'video profile. Check that ONVIF and RTSP are enabled.')

    stream_url = _select_stream_url(
        urls, camera_path, camera['ip'], camera_port)
    if not stream_url:
        return _result(
            False, 422,
            'The selected camera profile did not provide an RTSP stream URL.')

    authenticated_url = _inject_credentials(stream_url, username, password)
    width, height, _fps, rtsp_failure = probe_stream(
        authenticated_url, timeout=10, name='door validation')
    if width and height and rtsp_failure is None:
        return _result(True)

    if rtsp_failure == 'auth':
        return _result(
            False, 422,
            'Camera rejected the username or password for RTSP video. Check '
            'the camera credentials and try again.')
    if rtsp_failure == 'forbidden':
        return _result(
            False, 422,
            'Camera login succeeded, but this account cannot view the '
            'selected RTSP stream.')
    if rtsp_failure == 'not_found':
        return _result(
            False, 422,
            'The camera does not provide the selected RTSP stream path.')
    if rtsp_failure in ('refused', 'timeout', 'unreachable'):
        return _result(
            False, 503,
            'The camera accepted ONVIF login, but its RTSP stream is '
            'unreachable. Check that RTSP is enabled and the port is open.')
    return _result(
        False, 422,
        'The camera accepted ONVIF login, but the selected RTSP stream could '
        'not be decoded.')
