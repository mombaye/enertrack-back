from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        
        # Ajouter des données personnalisées dans le token
        token['role'] = user.role
        token['pays'] = user.pays
        token['username'] = user.username
        return token
