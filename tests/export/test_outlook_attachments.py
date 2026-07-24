from unittest.mock import patch

from src.export.outlook_attachments import (
    process_message_attachments,
)

SAMPLE_MESSAGE = {
    "Id": "msg1",
    "From": {"EmailAddress": {"Address": "boss@example.com"}},
    "Attachments": [
        {
            "@odata.type": "#Microsoft.OutlookServices.FileAttachment",
            "Id": "a1",
            "Name": "report.pdf",
            "ContentType": "application/pdf",
            "Size": 1234,
            "IsInline": False,
        },
        {
            "@odata.type": "#Microsoft.OutlookServices.FileAttachment",
            "Id": "a2",
            "Name": "img1.png",
            "ContentType": "image/png",
            "Size": 5678,
            "IsInline": True,
        },
        {
            "@odata.type": "#Microsoft.OutlookServices.ItemAttachment",
            "Id": "a3",
            "Name": "fwd.eml",
        },
        {
            "@odata.type": "#Microsoft.OutlookServices.ReferenceAttachment",
            "Id": "a4",
            "Name": "Doc.docx",
            "SourceUrl": "https://x.sharepoint.com/sites/foo/Doc.docx",
        },
    ],
}


@patch("src.export.outlook_attachments.fetch_sharepoint_link")
@patch("src.export.outlook_attachments.run_outlook_cli")
def test_dispatches_each_type_correctly(mock_cli, mock_sp, tmp_path):
    mock_cli.return_value = {
        "saved": [
            {
                "id": "a1",
                "name": "report.pdf",
                "path": str(tmp_path / "report.pdf"),
                "size": 1234,
            }
        ],
        "skipped": [],
    }
    from src.export.sharepoint_fetcher import SharepointFetchResult

    mock_sp.return_value = SharepointFetchResult(
        url="x",
        status="ok",
        local_path=tmp_path / "Doc.docx",
        file_name="Doc.docx",
        file_size=8888,
    )
    result = process_message_attachments(SAMPLE_MESSAGE, base_dir=tmp_path)

    # FileAttachments downloaded (regular + inline)
    assert mock_cli.called
    cli_args = mock_cli.call_args[0][0]
    assert cli_args[0] == "download-attachments"
    assert "--include-inline" in cli_args  # because there's an inline image

    # ItemAttachment skipped
    assert "fwd.eml" in result.skipped_item_attachments

    # ReferenceAttachment fetched via SharePoint
    assert mock_sp.called
    assert "Doc.docx" in result.fetched_sharepoint_files
