// The character library the @ list reads: the library module's own object, or nothing while that module is absent.
export async function openCastLibrary(deps) {
    try {
        const {createCastLibrary} = await import('./cast_library.js');
        return createCastLibrary(deps);
    } catch { return null; }
}
