import unittest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
import httpx
from services.kfood_ai import (
    _RECIPE_INSTRUCTION,
    _generation_cache_key,
    cache,
    router,
)


class KFoodAiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cache.clear()
        app = FastAPI()
        app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_question_contract(self):
        with patch("services.kfood_ai.generate", new=AsyncMock(return_value={"answer": "answer"})):
            response = await self.client.post("/ai/recipe/ask", json={
                "recipe_title": "Bibimbap", "question": "Can I use tofu?", "locale": "en"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"answer": "answer"})

    async def test_invalid_question(self):
        response = await self.client.post("/ai/recipe/ask", json={
            "recipe_title": "Bibimbap", "question": "", "locale": "xx"})
        self.assertEqual(response.status_code, 422)

    async def test_substitute_contract(self):
        rows = [{"substitute": "miso", "similarity_tier": "Similar",
                 "buy_difficulty": "Easy", "notes": "milder", "supermarket_hint": ""}]
        with patch("services.kfood_ai.generate", new=AsyncMock(return_value={"substitutes": rows})):
            response = await self.client.post("/ai/ingredient-substitutes", json={
                "ingredient": "doenjang", "country_code": "US", "country_name": "United States"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["substitutes"], rows)

    async def test_malformed_answer(self):
        with patch("services.kfood_ai.generate", new=AsyncMock(return_value={"answer": ""})):
            response = await self.client.post("/ai/recipe/ask", json={
                "recipe_title": "Bibimbap", "question": "Tofu?"})
        self.assertEqual(response.status_code, 502)

    async def test_daily_quota_and_bonus_limit(self):
        user = {'X-KFood-User': 'quota-test-user'}
        with patch("services.kfood_ai.generate", new=AsyncMock(return_value={"answer": "ok"})):
            for _ in range(2):
                response = await self.client.post(
                    "/ai/recipe/ask",
                    headers=user,
                    json={"recipe_title": "Bibimbap", "question": "How?"},
                )
                self.assertEqual(response.status_code, 200)
            self.assertEqual(
                (await self.client.post("/ai/recipe/reward", headers=user)).json()["granted"],
                True,
            )
            response = await self.client.post(
                "/ai/recipe/ask",
                headers=user,
                json={"recipe_title": "Bibimbap", "question": "How?"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                (await self.client.post("/ai/recipe/reward", headers=user)).json()["granted"],
                True,
            )
            response = await self.client.post(
                "/ai/recipe/ask",
                headers=user,
                json={"recipe_title": "Bibimbap", "question": "How?"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                (await self.client.post("/ai/recipe/reward", headers=user)).json()["granted"],
                False,
            )
            response = await self.client.post(
                "/ai/recipe/ask",
                headers=user,
                json={"recipe_title": "Bibimbap", "question": "How?"},
            )
            self.assertEqual(response.status_code, 429)

    async def test_cached_answer_does_not_consume_credit(self):
        user = {'X-KFood-User': 'cached-answer-user'}
        payload = {"recipe_title": "Bibimbap", "question": "How?", "locale": "en"}
        full_payload = {**payload, "recipe_category": "", "current_ingredients": []}
        cache.set(_generation_cache_key(full_payload, _RECIPE_INSTRUCTION), {"answer": "cached"})
        with patch("services.kfood_ai.generate", new=AsyncMock()) as generate_mock:
            response = await self.client.post("/ai/recipe/ask", headers=user, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate_mock.await_count, 0)
        usage = await self.client.get("/ai/recipe/usage", headers=user)
        self.assertEqual(usage.json()["free_used"], 0)

    async def test_failed_answer_rolls_back_credit(self):
        user = {'X-KFood-User': 'failed-answer-user'}
        with patch("services.kfood_ai.generate", new=AsyncMock(return_value={"answer": ""})):
            response = await self.client.post(
                "/ai/recipe/ask",
                headers=user,
                json={"recipe_title": "Bibimbap", "question": "How?"},
            )
        self.assertEqual(response.status_code, 502)
        usage = await self.client.get("/ai/recipe/usage", headers=user)
        self.assertEqual(usage.json()["free_used"], 0)
