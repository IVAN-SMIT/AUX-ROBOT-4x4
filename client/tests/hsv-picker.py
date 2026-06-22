"""HSV-пипетка: наведи мышь на цвет и смотри HSV."""
import cv2
import numpy as np

cap = cv2.VideoCapture(1)
cv2.namedWindow("HSV Picker")

def on_mouse(event, x, y, flags, param):
    if event == cv2.EVENT_MOUSEMOVE:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h, s, v = hsv[y, x]
        print(f"HSV: [{h}, {s}, {v}]", end="\r")

cv2.setMouseCallback("HSV Picker", on_mouse)

while True:
    ret, frame = cap.read()
    if not ret:
        break
    cv2.imshow("HSV Picker", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()