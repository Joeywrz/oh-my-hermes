import unittest

from omh.wrapper.localized_copy import (
    CARD_COPY_FIRST_PERSON_MARKERS,
    CARD_COPY_RECORD_TERMS,
    ChatCopy,
    card_copy_locales,
    card_copy_voice_violations,
    chat_copy,
    detect_copy_locale,
    is_localized_locale,
    prefers_korean_copy,
    skill_picker_body,
    skill_picker_headline,
)


class WrapperLocalizedCopyTests(unittest.TestCase):
    def test_locale_detection_is_local_and_deterministic(self) -> None:
        cases = {
            "OMH가 어떤 스킬 있는지 알려줘": "ko",
            "OMHで使えるスキルは？": "ja",
            "OMH 有哪些工作流？": "zh",
            "¿Qué comandos de OMH están disponibles?": "es",
            "Quelles commandes OMH sont disponibles ?": "fr",
            "Welche OMH Workflows gibt es?": "de",
            "what OMH workflows are available?": "en",
        }

        for message, locale in cases.items():
            with self.subTest(message=message):
                self.assertEqual(detect_copy_locale(message), locale)
                self.assertEqual(is_localized_locale(locale), locale != "en")

        self.assertTrue(prefers_korean_copy("OMH가 어떤 스킬 있는지 알려줘"))
        self.assertFalse(prefers_korean_copy("OMH 有哪些工作流？"))

    def test_core_cards_keep_multilingual_copy_in_catalog(self) -> None:
        cases = (
            ("img_summary", "en", "shareable image-card brief"),
            ("img_summary", "ja", "画像カード"),
            ("paper_learning", "fr", "paper-learning card"),
            ("source_finder", "zh", "source-finder plan"),
            ("web_research", "es", "research"),
            ("agent_ops_review", "ko", "관리자 관점"),
            ("workflow_learning_missed_route", "de", "missed-route feedback"),
            ("file_lookup", "ko", "파일/텍스트 확인"),
        )

        for copy_id, locale, expected_text in cases:
            with self.subTest(copy_id=copy_id, locale=locale):
                self.assertIn(expected_text, chat_copy(copy_id, locale=locale).body)

        self.assertIn("shareable image-card brief", chat_copy("img_summary", locale="unsupported").body)
        self.assertIn("이미지 안 문구", chat_copy("img_summary", korean=True).body)

    def test_skill_picker_copy_keeps_machine_contract_terms_visible(self) -> None:
        family_lines = ["- Plan and decide: deep-interview, ralplan."]

        english_body = skill_picker_body(catalog_question=True, locale="en", family_lines=family_lines)
        korean_body = skill_picker_body(catalog_question=True, locale="ko", family_lines=family_lines)
        japanese_body = skill_picker_body(catalog_question=True, locale="ja", family_lines=family_lines)
        chinese_body = skill_picker_body(catalog_question=True, locale="zh", family_lines=family_lines)

        self.assertEqual(skill_picker_headline(catalog_question=True, locale="en"), "Here are the OMH workflows.")
        self.assertEqual(skill_picker_headline(catalog_question=True, locale="ko"), "OMH workflow 목록입니다.")
        self.assertEqual(skill_picker_headline(catalog_question=True, locale="ja"), "OMH workflow 一覧です。")
        self.assertEqual(skill_picker_headline(catalog_question=True, locale="zh"), "这是 OMH workflow 列表。")
        self.assertIn("shell command", english_body)
        self.assertIn("shell 명령 승인을 받지 않아도", korean_body)
        self.assertIn("shell command", japanese_body)
        self.assertIn("shell command", chinese_body)
        self.assertIn("Route for me:", english_body)
        self.assertIn("Route for me:", korean_body)
        self.assertIn("Route for me:", japanese_body)
        self.assertIn("Route for me:", chinese_body)
        self.assertIn(family_lines[0], english_body)
        self.assertIn(family_lines[0], korean_body)
        self.assertIn(family_lines[0], japanese_body)
        self.assertIn(family_lines[0], chinese_body)


class CardCopyVoiceTests(unittest.TestCase):
    """The voice rule over `_CARD_COPY`, the table a wrapper renders verbatim.

    The scope is that table. Other producers of wrapper-facing text -- the
    operating-brief cards and inline headlines in `omh.wrapper.contract`, the
    session ladder in `omh.wrapper.sessions` -- are not subjects here and still
    carry the shapes these tests reject.
    """

    def test_card_copy_carries_no_record_term_or_listed_first_person_marker(self) -> None:
        self.assertEqual(card_copy_voice_violations(), ())

    def test_card_copy_keeps_its_locale_coverage_and_entry_count(self) -> None:
        locales = card_copy_locales()
        all_seven = ("de", "en", "es", "fr", "ja", "ko", "zh")

        self.assertEqual(
            locales,
            {
                "agent_ops_review": all_seven,
                "clarify": all_seven,
                "direct_answer": all_seven,
                "file_lookup": all_seven,
                "generic_clarify": all_seven,
                # The one card that is not seven-locale. Widening it is its
                # own change; this pin is here so a voice rewrite cannot drop
                # or quietly add a locale while moving every sentence.
                "goal_quality_coaching": ("en", "ko"),
                "img_summary": all_seven,
                "paper_learning": all_seven,
                "source_finder": all_seven,
                "web_research": all_seven,
                "workflow_learning_missed_route": all_seven,
                "workflow_learning_readiness": all_seven,
            },
        )
        self.assertEqual(sum(len(entry) for entry in locales.values()), 79)

    def test_every_record_term_is_caught_in_a_headline_and_in_a_body(self) -> None:
        # Three of the seven terms are absent from the table's history, so
        # without this case they would be a list nothing exercises.
        for term in CARD_COPY_RECORD_TERMS:
            with self.subTest(term=term):
                table = {
                    "probe": {
                        "en": ChatCopy(
                            headline=f"The {term} is ready.",
                            body=f"Open the {term} to continue.",
                        )
                    }
                }

                self.assertEqual(
                    card_copy_voice_violations(table),
                    (
                        ("probe", "en", "headline", "record_term", term),
                        ("probe", "en", "body", "record_term", term),
                    ),
                )

    def test_record_terms_are_caught_in_the_plural_and_inside_a_localized_body(self) -> None:
        table = {
            "probe": {
                "ko": ChatCopy(headline="준비됐습니다.", body="OMH workflow, picker, coding handoff는 열지 마세요."),
                "en": ChatCopy(headline="Two workflows are ready.", body="Fine."),
            }
        }

        self.assertEqual(
            card_copy_voice_violations(table),
            (
                ("probe", "en", "headline", "record_term", "workflow"),
                ("probe", "ko", "body", "record_term", "handoff"),
                ("probe", "ko", "body", "record_term", "picker"),
                ("probe", "ko", "body", "record_term", "workflow"),
            ),
        )

    def test_a_record_term_inside_a_longer_word_is_not_a_violation(self) -> None:
        # `lane` inside `planet` and `prepared` inside `preparedness` are
        # ordinary words. A checker that fires on them says nothing about the
        # sentences it passes, so the boundary is part of the rule.
        table = {
            "probe": {
                "en": ChatCopy(headline="The planet is large.", body="Preparedness is a different word."),
            }
        }

        self.assertEqual(card_copy_voice_violations(table), ())

    def test_each_locale_has_a_first_person_marker_that_fires(self) -> None:
        probes = {
            "en": "I will prepare this.",
            "ko": "정리하겠습니다.",
            "ja": "私が整理します。",
            "zh": "我会整理。",
            "es": "Puedo prepararlo.",
            "fr": "Je prépare cela.",
            "de": "Ich bereite das vor.",
        }
        self.assertEqual(set(probes), set(CARD_COPY_FIRST_PERSON_MARKERS))

        for locale, sentence in probes.items():
            with self.subTest(locale=locale):
                table = {"probe": {locale: ChatCopy(headline="Fine.", body=sentence)}}
                rules = {row[3] for row in card_copy_voice_violations(table)}

                self.assertIn("first_person", rules)

    def test_a_first_person_marker_is_not_charged_to_another_locale(self) -> None:
        # The markers are per locale on purpose: `i` standing alone is English
        # first person and `ich` is German, but neither says anything about a
        # Korean body, and charging them across locales would make the check
        # fire on loanwords the other languages borrow.
        table = {"probe": {"ko": ChatCopy(headline="확인이 필요합니다.", body="ich i me my 확인하세요.")}}

        self.assertEqual(card_copy_voice_violations(table), ())


if __name__ == "__main__":
    unittest.main()
