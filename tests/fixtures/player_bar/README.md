# Player-bar fixtures

Top 160 rows of 1920x1080 frames from the darwinprojectai Twitch VOD of
2026-09-05 (v2867196056), kept at their original pixel coordinates so the
detectors can be run on them unchanged. Ground truth was read off each full
frame by eye and cross-checked against the game's own alive counter in the
HUD.

| file | cards | alive (0-based indexes) | notes |
|---|---|---|---|
| vod_900.png  | 10 | 0, 4, 6 | blood-moon red tint; glitch card present at far left; card 4 is the spectated (enlarged) card |
| vod_2100.png | 10 | 0, 1, 3, 5, 7, 9 | card 9 spectated (enlarged) |
| vod_2700.png | 10 | 0, 5, 7 | blood-moon; card 7 spectated |
| vod_3300.png | 9  | all but 1 | director-panel layout (dark overlay bars under the cards); card 0 spectated |
| vod_3600.png | 9  | 2, 4, 5, 6, 7, 8 | snow; card 8 spectated |

`tests/test_player_cards_v2.py` asserts these. Add a row when you add a frame.
