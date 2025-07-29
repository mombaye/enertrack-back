from rest_framework_simplejwt.views import TokenObtainPairView
from users.serializers import CustomTokenObtainPairSerializer

class CustomLoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    def post(self, request, *args, **kwargs):
        # Log headers reçus
        print("---- HEADERS FRONTEND ----")
        for key, value in request.headers.items():
            print(f"{key}: {value}")
        print("--------------------------")
        # Optionnel : log le body reçu aussi
        print("Body:", request.data)
        return super().post(request, *args, **kwargs)

