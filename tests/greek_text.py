"""Text for the Greek OCR tests, all of it composed for them: no line comes from a mailbox.

GREEK and ENGLISH are ordinary prose. garble() passes text through LOOKALIKE, the substitution
an English-first OCR makes when it reads each Greek letter as the Latin letter it resembles.
"""

LOOKALIKE = str.maketrans(
    "αάβγδεέζηήθιίϊκλμνξοόπρσςτυύφχψωώ" + "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ",
    "aaByS" + "eezn" + "n0lll" + "kApvE" + "oomp" + "ogtuu" + "oxwww" + "ABrAEZHOIKAMNEOnPETYOXWQ",
)

GREEK = (
    "Η εταιρεία ανακοίνωσε την αύξηση των εσόδων για το τρίτο τρίμηνο και τα αποτελέσματα "
    "της χρήσης ήταν σύμφωνα με τις εκτιμήσεις της διοίκησης. Για την επόμενη περίοδο το νέο "
    "πρόγραμμα του έργου θα εξεταστεί από την επιτροπή, ενώ το κόστος είναι σταθερό."
)
ENGLISH = (
    "The quarterly report shows that revenue grew in the third quarter and that the results "
    "were in line with the estimates of management. For the next period the new programme "
    "will be reviewed by the committee, while costs remain stable."
)


def garble(text: str) -> str:
    return text.translate(LOOKALIKE)
