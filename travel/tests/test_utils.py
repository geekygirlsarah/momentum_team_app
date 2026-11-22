from django.test import SimpleTestCase

from travel.utils import travel_description_for_display


class TravelDescriptionDisplayTests(SimpleTestCase):
    def test_rich_text_preserves_supported_tags_and_safe_attributes(self):
        description = (
            "<p><strong>Schedule &amp; details</strong></p>"
            '<p><a href="https://example.test/trip?a=1&amp;b=2" '
            'target="_blank" title="Travel &quot;info&quot;">Read more</a></p>'
            '<p><span style="color: red;">Red</span></p>'
            '<br><img src="https://example.test/map.png" alt="Map">'
        )

        rendered = str(travel_description_for_display(description))

        self.assertIn("<strong>Schedule &amp; details</strong>", rendered)
        self.assertIn('href="https://example.test/trip?a=1&amp;b=2"', rendered)
        self.assertIn('target="_blank"', rendered)
        self.assertIn('title="Travel &quot;info&quot;"', rendered)
        self.assertIn('style="color:red;"', rendered)
        self.assertIn('<img src="https://example.test/map.png" alt="Map">', rendered)
        self.assertIn("<br>", rendered)

    def test_rich_text_escapes_text_and_removes_unsafe_markup(self):
        description = (
            "<p>5 &lt; 6 &amp; 7</p>"
            "<script>alert(1)</script>"
            '<a href="javascript:alert(1)" onclick="alert(1)">unsafe link</a>'
            '<img src="x" onerror="alert(1)">'
        )

        rendered = str(travel_description_for_display(description))

        self.assertIn("5 &lt; 6 &amp; 7", rendered)
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("javascript:", rendered.lower())
        self.assertNotIn("onclick", rendered.lower())
        self.assertNotIn("onerror", rendered.lower())

    def test_plain_text_keeps_line_breaks_and_is_html_escaped(self):
        rendered = str(travel_description_for_display("Line <one> & two\nNext line"))

        self.assertEqual(rendered, "Line &lt;one&gt; &amp; two<br>Next line")
