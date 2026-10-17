import QtQuick

// A QR code drawn from rows of "0"/"1" (see qr_code.py): dark modules on a white tile.
// Plain rectangles rather than a Canvas: a Canvas paints nothing on software-rendered setups.
Rectangle {
    id: tile
    property var rows: []
    property int size: 132
    readonly property int count: rows.length
    // The rows joined into one string, so a refresh that brings the same code changes nothing.
    readonly property string signature: rows.join("")
    // Whole pixels per module keep the edges sharp; the leftover space becomes the quiet zone.
    readonly property int cell: count ? Math.max(1, Math.floor((size - 12) / count)) : 0
    // Horizontal runs of dark modules: {x, y, n}.
    property var runs: []
    implicitWidth: size; implicitHeight: size
    radius: 8; color: "#ffffff"
    function rebuild() {
        var found = []
        for (var y = 0; y < rows.length; y++) {
            var row = rows[y], start = -1
            for (var x = 0; x <= row.length; x++) {
                var dark = x < row.length && row.charAt(x) === "1"
                if (dark && start < 0) start = x
                else if (!dark && start >= 0) { found.push({x: start, y: y, n: x - start}); start = -1 }
            }
        }
        runs = found
    }
    onSignatureChanged: rebuild()
    Component.onCompleted: rebuild()
    Item {
        anchors.centerIn: parent
        width: tile.cell * tile.count; height: width
        Repeater {
            model: tile.runs
            delegate: Rectangle {
                required property var modelData
                x: modelData.x * tile.cell; y: modelData.y * tile.cell
                width: modelData.n * tile.cell; height: tile.cell
                color: "#000000"; antialiasing: false
            }
        }
    }
}
