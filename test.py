a=[]
while True:
    number=input("enter the number")
    if number=="q":
        break
    try:
        number=int(number)
        a.append(number)
    except:
        print("enter a number")

#total=sum(a)
#average=total/len(a)
#print(sum)
#print(average)
total=0
for i in range(len(a)):
    total+=a[i]
print(total)
average=total/len(a)
print(average)
smallest=a[0]
for i in range(len(a)):
    try:
        
            
        if smallest > a[i]:
            smallest=a[i]
    except:
        pass

print(smallest)


    
