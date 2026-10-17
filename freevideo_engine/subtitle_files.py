"""Separate subtitle file (SRT or WebVTT) of a saved project's subtitle track.

Pure text out of the saved record; nothing is written to disk. The page offers
the reply as a download next to the export that burns the subtitles in.
"""
from . import editor_export as export
from . import subtitle_cues

FORMATS = {'srt': ('application/x-subrip', subtitle_cues.to_srt), 'vtt': ('text/vtt', subtitle_cues.to_vtt)}


def cues_of(project, start=0, frames=None):
    """Subtitle clips as cues, clipped to the range; times stay on the project's timeline."""
    result = []
    end = None if frames is None else start + frames
    for track in project.get('tracks', []):
        if track.get('kind') != 'subtitle' or track.get('hidden'):
            continue
        for clip in track['clips']:
            first, last = clip['start'], clip['start'] + clip['frames']
            if first < start:
                first = start
            if end is not None and last > end:
                last = end
            if last > first and clip['text'].strip():
                result.append(dict(start=first, frames=last - first, text=clip['text']))
    return result


def subtitle_file(project, fmt='srt', *, start=0, frames=None):
    """-> (media type, text). Times are shifted so the range starts at 00:00:00."""
    if fmt not in FORMATS:
        raise ValueError('Invalid subtitle format')
    if type(start) is not int or start < 0 or frames is not None and (type(frames) is not int or frames < 1):
        raise ValueError('Invalid range')
    media, write = FORMATS[fmt]
    return media, write(cues_of(project, start, frames), offset=start)


def register():
    from aiohttp import web
    from server import PromptServer
    server = PromptServer.instance
    if server is None or getattr(server, '_freevideo_subtitle_files', False):
        return
    server._freevideo_subtitle_files = True

    @server.routes.get('/freevideo/editor/subtitles')
    async def download(request):
        import asyncio
        query = request.query
        try:
            revision = query.get('revision')
            if revision is not None and not revision.isdecimal():
                raise ValueError('Invalid revision')
            numbers = {key: int(query[key]) for key in ('start', 'frames') if key in query and query[key].isdecimal()}
            if any(key in query and key not in numbers for key in ('start', 'frames')):
                raise ValueError('Invalid range')
            project = await asyncio.to_thread(export.load_project, query.get('id'),
                                              None if revision is None else int(revision))
            media, text = subtitle_file(project, query.get('format', 'srt'), start=numbers.get('start', 0),
                                        frames=numbers.get('frames'))
        except export.ExportConflict as error:
            return web.json_response({'revision': error.revision}, status=409, headers={'Cache-Control': 'no-store'})
        except FileNotFoundError:
            return web.json_response({'reason': 'failed'}, status=404)
        except (ValueError, TypeError, KeyError):
            return web.json_response({'reason': 'failed'}, status=400)
        return web.Response(text=text, content_type=media, charset='utf-8',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
