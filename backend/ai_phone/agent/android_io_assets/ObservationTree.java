package org.aiphone.observation;

import android.graphics.Rect;
import android.os.Build;
import android.os.SystemClock;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import org.xmlpull.v1.XmlSerializer;

/** Serializes the active public accessibility tree without the vendor's private dumper. */
final class ObservationTree {
    private static final int MAX_NODES = 2400;
    private static final int MAX_DEPTH = 64;
    private static final int MAX_VALUE = 512;
    private final long deadline = SystemClock.uptimeMillis() + 1200;
    private final XmlSerializer xml = Xml.newSerializer();
    private final Rect screen;
    private int count;
    private boolean partial;

    private ObservationTree(int width, int height) {
        screen = new Rect(0, 0, width, height);
    }

    static void write(AccessibilityNodeInfo root, File file, int rotation, int width, int height)
            throws IOException {
        ObservationTree tree = new ObservationTree(width, height);
        StringWriter nodes = new StringWriter();
        tree.xml.setOutput(nodes);
        tree.writeNode(root, 0, false);
        tree.xml.flush();
        String document = "<hierarchy rotation=\"" + rotation + "\" partial=\"" + tree.partial + "\">"
                + nodes + "</hierarchy>";
        try (FileOutputStream stream = new FileOutputStream(file)) {
            stream.write(document.getBytes(StandardCharsets.UTF_8));
        }
    }

    private void attribute(String key, CharSequence value) throws IOException {
        if (value == null || value.length() == 0) return;
        if (value.length() > MAX_VALUE) partial = true;
        StringBuilder safe = new StringBuilder();
        for (int index = 0; index < value.length() && index < MAX_VALUE;) {
            int code = Character.codePointAt(value, index);
            index += Character.charCount(code);
            if (code == 9 || code == 10 || code == 13 || (code >= 32 && code < 0xd800)
                    || (code >= 0xe000 && code < 0xfffe) || code >= 0x10000) safe.appendCodePoint(code);
        }
        if (safe.length() > 0) xml.attribute(null, key, safe.toString());
    }

    private void flag(String key, boolean value) throws IOException {
        xml.attribute(null, key, Boolean.toString(value));
    }

    private boolean exhausted(int depth) {
        return count >= MAX_NODES || depth >= MAX_DEPTH || SystemClock.uptimeMillis() >= deadline;
    }

    private void writeNode(AccessibilityNodeInfo node, int depth, boolean protectedParent) throws IOException {
        boolean protectedText = protectedParent || node.isPassword();
        Rect box = new Rect();
        node.getBoundsInScreen(box);
        if (depth > 0 && !Rect.intersects(screen, box)) return;
        if (!box.intersect(screen)) box.setEmpty();
        ++count;
        xml.startTag(null, "node");
        attribute("class", node.getClassName());
        attribute("package", node.getPackageName());
        attribute("resource-id", node.getViewIdResourceName());
        if (!protectedText) {
            attribute("text", node.getText());
            attribute("content-desc", node.getContentDescription());
        }
        if (Build.VERSION.SDK_INT >= 24) {
            attribute("drawing-order", Integer.toString(node.getDrawingOrder()));
        }
        if (Build.VERSION.SDK_INT >= 26 && !protectedParent) {
            attribute("hint", node.getHintText());
            flag("showing-hint-text", node.isShowingHintText());
        }
        flag("password", protectedText);
        flag("visible-to-user", depth == 0 || node.isVisibleToUser());
        flag("enabled", node.isEnabled());
        flag("clickable", node.isClickable());
        flag("editable", node.isEditable());
        flag("scrollable", node.isScrollable());
        flag("checkable", node.isCheckable());
        flag("checked", node.isChecked());
        flag("selected", node.isSelected());
        flag("focused", node.isFocused());
        attribute("bounds", box.toShortString());

        int children = node.getChildCount();
        boolean refreshed = false;
        for (int index = 0; index < children; ++index) {
            if (exhausted(depth + 1)) {
                partial = true;
                break;
            }
            AccessibilityNodeInfo child;
            try {
                child = node.getChild(index);
                if (child == null && !refreshed && !exhausted(depth + 1)) {
                    // A renderer may replace its virtual children as a click settles.
                    // Refresh that parent once instead of losing the rest of the frame.
                    refreshed = true;
                    if (node.refresh() && index < node.getChildCount()) child = node.getChild(index);
                }
            } catch (RuntimeException staleNode) {
                partial = true;
                continue;
            }
            if (child == null) {
                partial = true;
                continue;
            }
            try {
                if (child.isVisibleToUser()) writeNode(child, depth + 1, protectedText);
            } finally {
                child.recycle();
            }
        }
        xml.endTag(null, "node");
    }
}
