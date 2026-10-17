import QtQuick
import QtQuick.Controls.Basic

TextField {
    id: control
    // State updates must preserve the draft until the user submits it.
    property var value: undefined
    Binding {
        target: control; property: "text"
        value: control.value === undefined ? "" : control.value
        when: control.value !== undefined && !control.activeFocus
        restoreMode: Binding.RestoreNone
    }
    // Return keeps focus; sync after editingFinished commits or rejects input.
    onAccepted: Qt.callLater(function() {
        if (control.value !== undefined)
            control.text = control.value
    })
    implicitHeight: theme.height + 4
    color: theme.text; placeholderTextColor: theme.disabled
    selectionColor: theme.accentDim; selectedTextColor: theme.text
    font.pixelSize: theme.body
    leftPadding: 14; rightPadding: 14
    selectByMouse: true
    opacity: enabled ? 1 : 0.5
    background: Rectangle {
        radius: theme.radiusSm; color: theme.bg
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? theme.accent : control.hovered ? theme.sheen : theme.border
        Behavior on border.color { ColorAnimation { duration: 140 } }
    }
}
